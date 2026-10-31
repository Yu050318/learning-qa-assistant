"""Persistence queries for app/service/capabilities.py."""
from sqlalchemy import select
from sqlalchemy.engine import make_url
from app.entity.do.models import CapabilityConfiguration, CapabilityConfigurationVersion, User, utcnow
from app.dao.postgres.transactions import unwrap


def get_user_for_validation(session, uid):
    session = unwrap(session)
    return (session.scalar(select(User).where(User.id == uid).with_for_update(read=True).execution_options(populate_existing=True)))


def get_configuration(lock, session, shared):
    session = unwrap(session)
    query = select(CapabilityConfiguration).where(CapabilityConfiguration.id == 1).execution_options(populate_existing=True)
    return session.scalar(query.with_for_update(read=shared) if lock else query)


def connection_identity(database_url):
    return (make_url(database_url)._replace(password=None))


def configuration_history(limit, offset, session):
    session = unwrap(session)
    return (session.scalars(select(CapabilityConfigurationVersion).order_by(
                CapabilityConfigurationVersion.revision.desc()).offset(offset).limit(limit)).all())
