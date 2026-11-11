"""more block types

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-11 17:39:42.761235
"""
from alembic import op

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


OLD = ("heading", "paragraph", "list_item", "table", "figure", "caption", "footnote", "reference", "other")
NEW = ("heading", "paragraph", "list_item", "list", "table", "figure", "caption", "footnote", "reference",
       "callout", "equation", "other")


def _check(values: tuple[str, ...]) -> str:
    return "type IN (" + ", ".join(repr(v) for v in values) + ")"


def upgrade() -> None:
    op.drop_constraint("ck_blocks_type", "blocks", type_="check")
    op.create_check_constraint("ck_blocks_type", "blocks", _check(NEW))


def downgrade() -> None:
    op.execute("UPDATE blocks SET type = 'other' WHERE type IN ('list', 'callout', 'equation')")
    op.drop_constraint("ck_blocks_type", "blocks", type_="check")
    op.create_check_constraint("ck_blocks_type", "blocks", _check(OLD))
