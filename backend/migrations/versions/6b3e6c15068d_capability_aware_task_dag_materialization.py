"""capability aware task dag materialization

Revision ID: 6b3e6c15068d
Revises: 8f1d2c3b4a5e
Create Date: 2026-07-17 23:50:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "6b3e6c15068d"
down_revision: Union[str, Sequence[str], None] = "8f1d2c3b4a5e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("planning_session_id", sa.UUID(), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("planning_assignment_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_tasks_planning_session_id",
        "tasks",
        "goal_planning_sessions",
        ["planning_session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_tasks_planning_assignment_id",
        "tasks",
        "planning_assignments",
        ["planning_assignment_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_tasks_planning_session_id",
        "tasks",
        ["planning_session_id"],
    )

    op.add_column(
        "task_graph_revisions",
        sa.Column("planning_session_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "fk_task_graph_revisions_planning_session_id",
        "task_graph_revisions",
        "goal_planning_sessions",
        ["planning_session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_task_graph_revisions_planning_session",
        "task_graph_revisions",
        ["planning_session_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_task_graph_revisions_planning_session",
        "task_graph_revisions",
        type_="unique",
    )
    op.drop_constraint(
        "fk_task_graph_revisions_planning_session_id",
        "task_graph_revisions",
        type_="foreignkey",
    )
    op.drop_column("task_graph_revisions", "planning_session_id")

    op.drop_index("ix_tasks_planning_session_id", table_name="tasks")
    op.drop_constraint("fk_tasks_planning_assignment_id", "tasks", type_="foreignkey")
    op.drop_constraint("fk_tasks_planning_session_id", "tasks", type_="foreignkey")
    op.drop_column("tasks", "planning_assignment_id")
    op.drop_column("tasks", "planning_session_id")
