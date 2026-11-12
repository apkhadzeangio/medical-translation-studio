"""Human QA decisions and the academic refinement stage."""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_calls", sa.Column("prompt_version", sa.String(64), nullable=True))
    op.add_column("segments", sa.Column("refine_done", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("qa_flags", sa.Column("resolution_note", sa.Text(), nullable=True))
    op.add_column("qa_flags", sa.Column("resolved_by", sa.String(100), nullable=True))
    op.add_column("qa_flags", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint("ck_chapters_status", "chapters", type_="check")
    op.create_check_constraint("ck_chapters_status", "chapters",
                               "status IN ('pending','translating','reviewing','refining','qa','done','failed')")
    op.drop_constraint("ck_segment_versions_stage", "segment_versions", type_="check")
    op.create_check_constraint("ck_segment_versions_stage", "segment_versions",
                               "stage IN ('translate','review','refine','qa_fix','human')")


def downgrade() -> None:
    # Keep edited text and history readable by the older application.
    op.execute("UPDATE segment_versions SET stage = 'review' WHERE stage = 'refine'")
    op.execute("UPDATE chapters SET status = 'reviewing' WHERE status = 'refining'")
    op.drop_constraint("ck_segment_versions_stage", "segment_versions", type_="check")
    op.create_check_constraint("ck_segment_versions_stage", "segment_versions",
                               "stage IN ('translate','review','qa_fix','human')")
    op.drop_constraint("ck_chapters_status", "chapters", type_="check")
    op.create_check_constraint("ck_chapters_status", "chapters",
                               "status IN ('pending','translating','reviewing','qa','done','failed')")
    op.drop_column("qa_flags", "resolved_at")
    op.drop_column("qa_flags", "resolved_by")
    op.drop_column("qa_flags", "resolution_note")
    op.drop_column("segments", "refine_done")
    op.drop_column("llm_calls", "prompt_version")
