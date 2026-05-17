"""One-shot migration runner for Phase 1 and later admin IA reorg changes.

Applies the schema + data changes documented in migrations/versions/*.py
using pure stdlib `sqlite3` because SQLAlchemy / Alembic startup is
prohibitively slow on the dev machine, and we want prod auto-bootstrap to
be fast and dependency-light on every boot.

Currently applies:
  - 0001 -> 0002 (Phase 1: BU schema + Software->Product + unified links)
  - 0002 -> 0003 (Phase 5b: chat_question_cluster table)

Idempotent: re-running on a fully-migrated DB is a fast no-op.

Future migrations can either add a new step here or be applied via the
regular Alembic CLI (`alembic upgrade head`) once Alembic's import speed
isn't a blocker.

Usage:
    python scripts/apply_phase_1_migration.py [--db PATH]
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

    # --- preflight: check alembic_version, then sanity-check schema ---
    HEAD = '0003_chat_question_clusters'
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'"
    )
    if cur.fetchone():
        v_row = cur.execute("SELECT version_num FROM alembic_version").fetchone()
        current_version = v_row[0] if v_row else None
        if current_version == HEAD:
            # Sanity check: the version row claims we're at HEAD, but if a
            # prior run stamped the version while a step silently failed,
            # the schema may be lying. Check a couple of canary columns
            # that MUST exist if Phase 1 fully ran. If they don't, force a
            # re-run so the migration is genuinely idempotent under
            # partial-failure conditions.
            cur.execute("PRAGMA table_info(product_link)")
            pl_cols = {row[1] for row in cur.fetchall()}
            cur.execute("PRAGMA table_info(business_line)")
            bl_cols = {row[1] for row in cur.fetchall()}
            schema_ok = (
                'description' in pl_cols
                and 'product_type' in bl_cols
                and 'signup_form_url' not in bl_cols  # dropped in Phase 1
            )
            if schema_ok:
                print(f'DB already at {HEAD} and schema matches. Nothing to do.')
                return 0
            print(
                f'!!! DB stamped at {HEAD} but schema sanity check FAILED '
                f'(product_link.description in pl_cols: '
                f'{"description" in pl_cols}, '
                f'business_line.product_type in bl_cols: '
                f'{"product_type" in bl_cols}). '
                f'Re-running migration to repair.'
            )
        else:
            print(f'alembic_version present at {current_version}; running pending migrations to {HEAD}.')
    else:
        # Create alembic_version table and stamp at baseline.
        cur.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY)"
        )
        cur.execute("INSERT INTO alembic_version (version_num) VALUES ('0001_baseline')")
        current_version = '0001_baseline'
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
    # This step is split into idempotent sub-steps so a partial earlier
    # failure self-heals on rerun. The sub-steps:
    #   4a. Ensure description column exists (NULL-able first).
    #   4b. Ensure note column exists.
    #   4c. If kind/label exist, backfill description from them.
    #   4d. If kind/label exist, rebuild table to drop them + make
    #       description NOT NULL.
    #   4e. If table is already in target shape (no kind/label,
    #       description exists), make sure description has no NULL rows
    #       (would block a future NOT NULL constraint).
    cur.execute("PRAGMA table_info(product_link)")
    pl_cols = {row[1] for row in cur.fetchall()}
    print(f'Step 4: product_link cols before: {sorted(pl_cols)}')

    if 'description' not in pl_cols:
        cur.execute("ALTER TABLE product_link ADD COLUMN description VARCHAR(200)")
        print('Step 4a: added product_link.description (nullable).')
        pl_cols.add('description')
    if 'note' not in pl_cols:
        cur.execute("ALTER TABLE product_link ADD COLUMN note TEXT")
        print('Step 4b: added product_link.note.')
        pl_cols.add('note')

    has_kind = 'kind' in pl_cols
    has_label = 'label' in pl_cols

    if has_kind or has_label:
        # 4c: backfill description from kind/label for any row that doesn't
        # already have one. Composes "[kind] label" / "label" / "kind" /
        # falls back to a literal "Link" so the NOT NULL constraint passes.
        if has_kind and has_label:
            sql = ("UPDATE product_link SET description = "
                   "CASE "
                   "  WHEN kind IS NOT NULL AND kind != '' "
                   "       AND label IS NOT NULL AND label != '' "
                   "    THEN '[' || kind || '] ' || label "
                   "  WHEN label IS NOT NULL AND label != '' THEN label "
                   "  WHEN kind IS NOT NULL AND kind != '' THEN kind "
                   "  ELSE 'Link' "
                   "END "
                   "WHERE description IS NULL OR description = ''")
        elif has_label:
            sql = ("UPDATE product_link SET description = "
                   "COALESCE(NULLIF(label, ''), 'Link') "
                   "WHERE description IS NULL OR description = ''")
        else:  # has_kind only
            sql = ("UPDATE product_link SET description = "
                   "COALESCE(NULLIF(kind, ''), 'Link') "
                   "WHERE description IS NULL OR description = ''")
        cur.execute(sql)
        print(f'Step 4c: backfilled product_link.description from kind/label.')

        # 4d: rebuild table to drop kind+label and add NOT NULL constraint.
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
        print('Step 4d: rebuilt product_link (dropped kind, label, NOT NULL description).')
    else:
        # 4e: kind/label are already gone. Ensure no NULL descriptions
        # remain so the model's NOT NULL constraint is satisfied.
        cur.execute(
            "UPDATE product_link SET description = 'Link' "
            "WHERE description IS NULL OR description = ''"
        )
        print('Step 4e: backfilled any NULL product_link.description rows to "Link".')

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

    # --- 7. stamp at Phase 1 revision ---
    cur.execute("UPDATE alembic_version SET version_num = '0002_phase_1_ia_reorg'")
    print('Stamped alembic_version at 0002_phase_1_ia_reorg.')

    # --- 8. Phase 5b: create chat_question_cluster table -----------------
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='chat_question_cluster'"
    )
    if not cur.fetchone():
        cur.execute(
            "CREATE TABLE chat_question_cluster ("
            " id INTEGER PRIMARY KEY,"
            " title VARCHAR(200) NOT NULL,"
            " representative_question TEXT NOT NULL,"
            " sample_questions TEXT NOT NULL,"
            " count INTEGER NOT NULL DEFAULT 0,"
            " first_seen_at DATETIME,"
            " last_seen_at DATETIME,"
            " promoted_to_faq_id INTEGER,"
            " created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,"
            " FOREIGN KEY (promoted_to_faq_id) REFERENCES faq (id) ON DELETE SET NULL"
            ")"
        )
        print('Created chat_question_cluster table.')
    else:
        print('chat_question_cluster table already exists, skipping.')

    cur.execute("UPDATE alembic_version SET version_num = '0003_chat_question_clusters'")
    print('Stamped alembic_version at 0003_chat_question_clusters.')

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
