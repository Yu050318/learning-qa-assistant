"""Persistence queries for app/service/model_center.py."""
from sqlalchemy import case, func, inspect, select
from app.entity.do.models import ModelCall, ModelConfiguration, ModelConfigurationVersion, User, utcnow
from app.dao.postgres.transactions import unwrap


def history_table_missing(session):
    session = unwrap(session)
    return (not inspect(session.connection()).has_table("model_configuration_versions", schema="rag"))


def get_configuration(lock, session):
    session = unwrap(session)
    query = select(ModelConfiguration).where(ModelConfiguration.id == 1)
    return session.scalar(query.with_for_update() if lock else query)


def profile_was_used(key, previous, session):
    session = unwrap(session)
    return (previous is None and session.scalar(select(ModelConfigurationVersion.revision).where(
                        ModelConfigurationVersion.configuration["profiles"][key].as_string().is_not(None)).limit(1)) is not None)


def configuration_history(limit, offset, session):
    session = unwrap(session)
    return (session.scalars(select(ModelConfigurationVersion).order_by(ModelConfigurationVersion.revision.desc())
                                   .offset(offset).limit(limit)).all())


def history_page(rows, session):
    session = unwrap(session)
    return ({"items": [{"revision": row.revision, "created_by": row.created_by, "created_at": row.created_at,
                               "configuration": row.configuration} for row in rows],
                    "total": session.scalar(select(func.count()).select_from(ModelConfigurationVersion))})


def aggregate_usage(days, session, uid=None):
    from datetime import timedelta
    session = unwrap(session)
    query = select(ModelCall.provider, ModelCall.model_name, ModelCall.model_role, ModelCall.model_profile_id, ModelCall.model_profile_version, func.count().label('attempts'), func.sum(case((ModelCall.status == 'succeeded', 1), else_=0)).label('succeeded'), func.sum(case((ModelCall.status == 'failed', 1), else_=0)).label('failed'), func.sum(case((ModelCall.status == 'started', 1), else_=0)).label('incomplete'), func.sum(case((ModelCall.fallback.is_(True), 1), else_=0)).label('fallback_attempts'), func.count(ModelCall.total_tokens).label('reported_calls'), func.sum(ModelCall.input_tokens).label('reported_input_tokens'), func.sum(ModelCall.output_tokens).label('reported_output_tokens'), func.sum(ModelCall.total_tokens).label('reported_total_tokens'), func.avg(ModelCall.elapsed_ms).label('average_ms')).where(ModelCall.created_at >= utcnow() - timedelta(days=days))
    if uid is not None:
        query = query.where(ModelCall.user_id == uid)
    return [dict(row._mapping) for row in session.execute(query.group_by(ModelCall.provider, ModelCall.model_name, ModelCall.model_role, ModelCall.model_profile_id, ModelCall.model_profile_version))]
