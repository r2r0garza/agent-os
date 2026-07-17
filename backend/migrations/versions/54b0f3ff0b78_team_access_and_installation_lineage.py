"""team access and installation lineage

Revision ID: 54b0f3ff0b78
Revises: 7d2a9c4e1b6f
Create Date: 2026-07-16 19:16:06.281666

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '54b0f3ff0b78'
down_revision: Union[str, Sequence[str], None] = '7d2a9c4e1b6f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


team_member_role = postgresql.ENUM(
    "owner", "member", name="team_member_role", create_type=False
)

DEFAULT_TEAM_NAME = "Default Team"
DEFAULT_USER_EMAIL = "operator@local"


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    team_member_role.create(bind, checkfirst=True)

    op.create_table('skill_installations',
    sa.Column('installed_skill_id', sa.UUID(), nullable=False),
    sa.Column('source_skill_version_id', sa.UUID(), nullable=False),
    sa.Column('installed_by', sa.UUID(), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['installed_by'], ['users.id'], name=op.f('fk_skill_installations_installed_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['installed_skill_id'], ['skills.id'], name=op.f('fk_skill_installations_installed_skill_id_skills'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_skill_version_id'], ['skill_versions.id'], name=op.f('fk_skill_installations_source_skill_version_id_skill_versions'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_skill_installations')),
    sa.UniqueConstraint('installed_skill_id', name='uq_skill_installations_installed_skill')
    )
    op.create_table('agent_installations',
    sa.Column('installed_agent_id', sa.UUID(), nullable=False),
    sa.Column('source_agent_version_id', sa.UUID(), nullable=False),
    sa.Column('installed_by', sa.UUID(), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['installed_agent_id'], ['agents.id'], name=op.f('fk_agent_installations_installed_agent_id_agents'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['installed_by'], ['users.id'], name=op.f('fk_agent_installations_installed_by_users'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['source_agent_version_id'], ['agent_versions.id'], name=op.f('fk_agent_installations_source_agent_version_id_agent_versions'), ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_installations')),
    sa.UniqueConstraint('installed_agent_id', name='uq_agent_installations_installed_agent')
    )
    op.add_column('project_members', sa.Column('granted_by', sa.UUID(), nullable=True))
    op.create_foreign_key(op.f('fk_project_members_granted_by_users'), 'project_members', 'users', ['granted_by'], ['id'], ondelete='SET NULL')
    op.add_column('tasks', sa.Column('created_by', sa.UUID(), nullable=True))
    op.create_foreign_key(op.f('fk_tasks_created_by_users'), 'tasks', 'users', ['created_by'], ['id'], ondelete='SET NULL')
    op.add_column('team_memberships', sa.Column('role', team_member_role, server_default='member', nullable=False))

    # Backfill: the pre-Sprint-8 bootstrap path created the default team and
    # default user as independent rows with no membership linking them, so
    # access checks that walk `team_memberships` found nothing for the local
    # operator. Make that relationship explicit wherever it already exists,
    # rather than leaving it a hidden singleton assumption.
    team_row = bind.execute(
        sa.text("SELECT id FROM teams WHERE name = :name"), {"name": DEFAULT_TEAM_NAME}
    ).fetchone()
    user_row = bind.execute(
        sa.text("SELECT id FROM users WHERE email = :email"), {"email": DEFAULT_USER_EMAIL}
    ).fetchone()
    if team_row is not None and user_row is not None:
        membership_row = bind.execute(
            sa.text(
                "SELECT id FROM team_memberships WHERE team_id = :team_id AND user_id = :user_id"
            ),
            {"team_id": team_row.id, "user_id": user_row.id},
        ).fetchone()
        if membership_row is None:
            bind.execute(
                sa.text(
                    "INSERT INTO team_memberships (id, team_id, user_id, role, created_at) "
                    "VALUES (gen_random_uuid(), :team_id, :user_id, 'owner', now())"
                ),
                {"team_id": team_row.id, "user_id": user_row.id},
            )
        else:
            bind.execute(
                sa.text("UPDATE team_memberships SET role = 'owner' WHERE id = :id"),
                {"id": membership_row.id},
            )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('team_memberships', 'role')
    op.drop_constraint(op.f('fk_tasks_created_by_users'), 'tasks', type_='foreignkey')
    op.drop_column('tasks', 'created_by')
    op.drop_constraint(op.f('fk_project_members_granted_by_users'), 'project_members', type_='foreignkey')
    op.drop_column('project_members', 'granted_by')
    op.drop_table('agent_installations')
    op.drop_table('skill_installations')

    bind = op.get_bind()
    team_member_role.drop(bind, checkfirst=True)
