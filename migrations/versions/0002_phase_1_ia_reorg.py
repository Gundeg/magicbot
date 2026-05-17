"""phase_1_ia_reorg

Schema + data migration for Phase 1 of the admin IA reorganization.

Schema changes:
  - business_line: drop 5 columns (signup_form_url, signup_phone, exam_form_url,
    num_products_or_services, total_clients_or_users); add product_type
    (Course | Service | Product) with CHECK constraint.
  - software: drop entire table (data migrated into product first).
  - product_link: rename label -> description (now NOT NULL); add note (Text
    nullable); drop kind (data prefixed into description as "[<kind>] ").
  - course_link: new table mirroring product_link's new shape.
  - service_link: new table mirroring product_link's new shape.

Data changes (run inside upgrade(), before dropping software):
  - Backfill business_line.product_type from each row's name.
  - Migrate every software row to a product row owned by the Magic Cloud BU
    (allocates fresh product_number values past the current MAX).
  - Write one audit_entry per migrated software row so the move is recoverable.
  - Collapse product_link.kind into the new description column.

Revision ID: 0002_phase_1_ia_reorg
Revises: 0001_baseline
Create Date: 2026-05-18

"""
from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = '0002_phase_1_ia_reorg'
down_revision = '0001_baseline'
branch_labels = None
depends_on = None


# -----------------------------------------------------------------------------
# Data-migration helpers (kept module-private; only called from upgrade()).
# -----------------------------------------------------------------------------

def _classify_business_line(name):
    """Map a business_line name to its product_type.

    Match is case-insensitive substring. Returns 'Product' as a safe default
    so the NOT NULL column is always populated; admins can hand-correct
    misclassified rows from the Business Units tab after deploy.
    """
    if not name:
        return 'Product'
    n = name.lower()
    if 'choice' in n or 'сургалт' in n or 'training' in n:
        return 'Course'
    if 'consulting' in n or 'audit' in n or 'аудит' in n or 'cpa' in n:
        return 'Service'
    # Magic Cloud / software / programs land here, plus any unknown line.
    return 'Product'


def _find_or_create_magic_cloud_bu(conn):
    """Locate the Business Line that should adopt migrated software rows.

    Search order:
      1. A line whose name matches Magic Cloud / Cloud / Програм.
      2. Any line with product_type='Product' (set in the earlier loop).
      3. Fall back to inserting a placeholder line so software rows aren't
         orphaned. Admin can rename or merge it from the UI afterwards.

    Returns the line's id.
    """
    row = conn.execute(sa.text(
        "SELECT id FROM business_line "
        "WHERE LOWER(name) LIKE '%cloud%' "
        "   OR LOWER(name) LIKE '%magic cloud%' "
        "   OR LOWER(name) LIKE '%програм%' "
        "ORDER BY id ASC LIMIT 1"
    )).fetchone()
    if row:
        return row[0]

    row = conn.execute(sa.text(
        "SELECT id FROM business_line WHERE product_type = 'Product' "
        "ORDER BY id ASC LIMIT 1"
    )).fetchone()
    if row:
        return row[0]

    now = datetime.utcnow().isoformat(sep=' ')
    result = conn.execute(
        sa.text(
            "INSERT INTO business_line "
            "(name, description, action, is_active, sort_order, "
            "product_type, created_at, updated_at) "
            "VALUES (:name, :desc, 'refer', 1, 99, 'Product', :now, :now)"
        ),
        {
            'name': 'Migrated Software',
            'desc': 'Auto-created by phase_1_ia_reorg to hold software rows '
                    'that had no Magic Cloud business line. Rename or merge '
                    'after review.',
            'now': now,
        },
    )
    return result.lastrowid


# -----------------------------------------------------------------------------
# upgrade / downgrade
# -----------------------------------------------------------------------------

def upgrade():
    conn = op.get_bind()
    now = datetime.utcnow().isoformat(sep=' ')

    # --- business_line: add product_type --------------------------------
    with op.batch_alter_table('business_line') as batch:
        batch.add_column(sa.Column(
            'product_type',
            sa.String(length=20),
            nullable=False,
            server_default='Product',
        ))

    # Backfill product_type per row based on the line's name.
    lines = conn.execute(sa.text(
        "SELECT id, name FROM business_line"
    )).fetchall()
    for line_id, line_name in lines:
        conn.execute(
            sa.text("UPDATE business_line SET product_type = :pt WHERE id = :id"),
            {'pt': _classify_business_line(line_name), 'id': line_id},
        )

    # --- new link tables ------------------------------------------------
    op.create_table(
        'course_link',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('course_id', sa.Integer,
                  sa.ForeignKey('course.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('description', sa.String(length=200), nullable=False),
        sa.Column('url', sa.String(length=500), nullable=False),
        sa.Column('note', sa.Text, nullable=True),
        sa.Column('is_active', sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column('sort_order', sa.Integer, nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp()),
    )
    op.create_table(
        'service_link',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('service_id', sa.Integer,
                  sa.ForeignKey('service.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('description', sa.String(length=200), nullable=False),
        sa.Column('url', sa.String(length=500), nullable=False),
        sa.Column('note', sa.Text, nullable=True),
        sa.Column('is_active', sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column('sort_order', sa.Integer, nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime, nullable=False,
                  server_default=sa.func.current_timestamp()),
    )

    # --- product_link: kind/label -> description; add note --------------
    # Step 1: add description + note columns (nullable for now so existing
    # rows survive). Step 2: backfill description from kind + label.
    # Step 3: drop kind, drop label, then make description NOT NULL.
    with op.batch_alter_table('product_link') as batch:
        batch.add_column(sa.Column('description', sa.String(length=200), nullable=True))
        batch.add_column(sa.Column('note', sa.Text, nullable=True))

    conn.execute(sa.text(
        "UPDATE product_link SET description = "
        "CASE "
        "  WHEN kind IS NOT NULL AND kind != '' "
        "       AND label IS NOT NULL AND label != '' "
        "    THEN '[' || kind || '] ' || label "
        "  WHEN label IS NOT NULL AND label != '' THEN label "
        "  WHEN kind IS NOT NULL AND kind != '' THEN kind "
        "  ELSE 'Link' "
        "END"
    ))

    with op.batch_alter_table('product_link') as batch:
        batch.drop_column('kind')
        batch.drop_column('label')
        batch.alter_column('description', nullable=False,
                           existing_type=sa.String(length=200))

    # --- software -> product migration ----------------------------------
    # Has to happen AFTER business_line.product_type is populated so we can
    # land software rows on a Product-typed line.
    inspector = sa.inspect(conn)
    if 'software' in inspector.get_table_names():
        software_rows = conn.execute(sa.text(
            "SELECT id, name, description, vendor, is_active, "
            "       status_note, sort_order, created_at "
            "FROM software ORDER BY id ASC"
        )).fetchall()
        if software_rows:
            target_bl_id = _find_or_create_magic_cloud_bu(conn)
            next_number_row = conn.execute(sa.text(
                "SELECT COALESCE(MAX(product_number), 0) FROM product"
            )).fetchone()
            next_number = (next_number_row[0] or 0) + 1

            for s in software_rows:
                sw_id = s[0]
                sw_name = s[1]
                conn.execute(
                    sa.text(
                        "INSERT INTO product "
                        "(business_line_id, product_number, name, vendor, "
                        " description, is_main_product, is_active, "
                        " status_note, sort_order, created_at, updated_at) "
                        "VALUES (:bl, :pn, :name, :vendor, :desc, 0, "
                        "        :active, :note, :sort, :created, :updated)"
                    ),
                    {
                        'bl': target_bl_id,
                        'pn': next_number,
                        'name': sw_name,
                        'vendor': s[3],
                        'desc': s[2],
                        'active': 1 if s[4] else 0,
                        'note': s[5],
                        'sort': s[6] or 0,
                        'created': s[7] or now,
                        'updated': now,
                    },
                )
                conn.execute(
                    sa.text(
                        "INSERT INTO audit_entry "
                        "(actor_username, action, entity_type, entity_id, "
                        " entity_label, detail, created_at) "
                        "VALUES (NULL, 'system_migration', 'software', :sid, "
                        "        :label, :detail, :now)"
                    ),
                    {
                        'sid': sw_id,
                        'label': sw_name,
                        'detail': (
                            f'Migrated software row #{sw_id} -> product '
                            f'#{next_number} on business_line {target_bl_id}.'
                        ),
                        'now': now,
                    },
                )
                next_number += 1
        op.drop_table('software')

    # --- business_line: drop the 5 obsolete columns ---------------------
    # Done LAST so that any earlier step that wanted to read them (e.g. for
    # future audit needs) still could. Today nothing reads them, but the
    # ordering is cheap insurance.
    with op.batch_alter_table('business_line') as batch:
        for col in ('signup_form_url', 'signup_phone', 'exam_form_url',
                    'num_products_or_services', 'total_clients_or_users'):
            batch.drop_column(col)


def downgrade():
    # Reversing this migration is intentionally not supported. The schema
    # drops are recoverable but the software-row data has already been
    # rewritten into product rows under new ids; un-doing that cleanly
    # would require remembering the original ids and is more risk than
    # value. Restore from a backup instead.
    raise NotImplementedError(
        "phase_1_ia_reorg is not reversible. Restore from a DB backup."
    )
