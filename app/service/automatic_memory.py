"""Opt-in policy and a durable, fenced personal-memory synchronization workflow."""
from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from uuid import UUID

from app.core.errors import AppError, not_found
from app.dao.postgres import automatic_memory as dao
from app.entity.do.models import AutomaticMemory, utcnow
from app.entity.dto.memory import MemoryHit, MemorySettingsState
from app.entity.time import aware
from app.manager.transactions import transaction_scope

logger = logging.getLogger('rag')
PENDING = {'pending', 'running', 'waiting'}


class AutomaticMemoryService:
    def __init__(self, settings, database, manager):
        self.settings, self.database, self.manager = settings, database, manager

    def require(self):
        if not self.settings.openviking_enabled:
            raise AppError('AUTOMATIC_MEMORY_DISABLED', '自动记忆服务尚未启用', 503)

    def get_settings(self, user_id: UUID) -> MemorySettingsState:
        if not self.settings.openviking_enabled:
            return {'available': False, 'automatic_enabled': False, 'generation': 1, 'pending_jobs': 0, 'failed_jobs': 0}
        with transaction_scope(self.database) as transaction:
            row = dao.settings(transaction, user_id)
            generation = row.generation if row else 1
            pending, failed = dao.job_counts(transaction, user_id, generation)
            return {'available': True, 'automatic_enabled': bool(row and row.enabled),
                    'generation': generation, 'pending_jobs': pending, 'failed_jobs': failed}

    def set_enabled(self, user_id: UUID, enabled: bool) -> MemorySettingsState:
        self.require()
        with transaction_scope(self.database) as transaction:
            row = dao.settings(transaction, user_id, create=True, lock=True)
            if row.enabled != enabled:
                if not enabled:
                    self._clear(transaction, user_id, row)
                row.enabled, row.enabled_since = enabled, utcnow() if enabled else None
                transaction.commit()
        return self.get_settings(user_id)

    @staticmethod
    def _clear(transaction, user_id, consent):
        old = consent.generation
        consent.generation += 1
        for item in dao.memories(transaction, user_id, old, include_deleted=True):
            item.state, item.content = 'deleting', ''
        dao.enqueue(transaction, user_id, old, 'purge', 'purge', {'phase': 'new'})

    def clear(self, user_id: UUID) -> MemorySettingsState:
        self.require()
        with transaction_scope(self.database) as transaction:
            consent = dao.settings(transaction, user_id, create=True, lock=True)
            self._clear(transaction, user_id, consent)
            consent.enabled_since = utcnow() if consent.enabled else None
            transaction.commit()
        return self.get_settings(user_id)

    def list(self, user_id: UUID) -> list[AutomaticMemory]:
        if not self.settings.openviking_enabled:
            return []
        with transaction_scope(self.database) as transaction:
            row = dao.settings(transaction, user_id)
            return dao.memories(transaction, user_id, row.generation) if row else []

    def delete(self, user_id: UUID, memory_id: UUID) -> None:
        self.require()
        with transaction_scope(self.database) as transaction:
            consent = dao.settings(transaction, user_id, lock=True)
            item = dao.memory(transaction, user_id, memory_id)
            if not item or not consent or item.generation != consent.generation:
                raise not_found()
            item.state, item.content = 'deleting', ''
            dao.enqueue(transaction, user_id, item.generation, 'delete', 'delete/' + str(item.id),
                        {'phase': 'new', 'uri': item.uri, 'memory_id': str(item.id)})
            transaction.commit()

    def search(self, user_id: UUID, query: str) -> tuple[list[MemoryHit], list[str]]:
        if not self.settings.openviking_enabled:
            return [], []
        with transaction_scope(self.database) as transaction:
            consent = dao.settings(transaction, user_id)
            if not consent or not consent.enabled:
                return [], []
            generation = consent.generation
            if not dao.memories(transaction, user_id, generation):
                return [], []
        try:
            hits = self.manager.find(user_id, generation, query, self.settings.memory_top_k)
        except AppError as error:
            logger.warning('automatic_memory_recall_failed', extra={'error_code': error.code})
            return [], [error.code]
        # Recheck after the network call: disable/delete may have raced with recall.
        with transaction_scope(self.database) as transaction:
            consent = dao.settings(transaction, user_id)
            if not consent or not consent.enabled or consent.generation != generation:
                return [], []
            rows = {m.uri: m for m in dao.memories(transaction, user_id, generation)}
            times = dao.source_times(transaction, user_id, [m.source_message_id for m in rows.values() if m.source_message_id])
            return [{'memory_id': rows[uri].id, 'content': rows[uri].content[:12000],
                     'version': rows[uri].version, 'score': score,
                     'asserted_at': aware(times.get(rows[uri].source_message_id, rows[uri].updated_at)).isoformat()}
                    for uri, score in hits if uri in rows], []

    def validate_citations(self, transaction, user_id, citations):
        if not self.settings.openviking_enabled:
            return
        consent = dao.settings(transaction, user_id, lock=True)
        for citation in citations:
            if citation.get('type') != 'memory':
                continue
            item = dao.memory(transaction, user_id, UUID(str(citation['memory_id'])))
            if item and (not consent or not consent.enabled or item.generation != consent.generation or item.state != 'ready'):
                raise AppError('MEMORY_SCOPE_CHANGED', '回答期间使用的自动记忆已被关闭或删除，请重新提问', 409)

    def _save_progress(self, job, receipt, state='running', error_code=None):
        with transaction_scope(self.database) as transaction:
            current = dao.owned(transaction, job)
            current.receipt, current.state, current.error_code = receipt, state, error_code
            if state == 'running':
                current.lease_until = dao.now(transaction) + timedelta(seconds=180)
            if state != 'running':
                current.lease_until = None
                current.available_at = utcnow() + timedelta(seconds=self.settings.openviking_poll_seconds)
            transaction.commit()
        job.receipt = receipt

    def _publish(self, job, operations):
        with transaction_scope(self.database) as transaction:
            current = dao.owned(transaction, job)
            consent = dao.settings(transaction, job.user_id, lock=True)
            if not consent or not consent.enabled or consent.generation != job.generation:
                current.state, current.lease_until = 'cancelled', None
                transaction.commit()
                return
            rows = {m.uri: m for m in dao.memories(transaction, job.user_id, job.generation, include_deleted=True)}
            for action in ('adds', 'updates', 'deletes'):
                changes = operations.get(action, [])
                if not isinstance(changes, list) or len(changes) > 200:
                    raise AppError('OPENVIKING_RESPONSE_INVALID', '记忆变更数量无效', 502)
                for change in changes:
                    if not isinstance(change, dict):
                        raise AppError('OPENVIKING_RESPONSE_INVALID', '记忆变更记录无效', 502)
                    uri = self.manager.memory_uri(job.user_id, job.generation, change.get('uri'))
                    item = rows.get(uri)
                    if action == 'deletes':
                        if item:
                            item.state, item.content = 'deleted', ''
                        continue
                    # Tombstones prevent queued extraction from resurrecting deleted files.
                    if item and item.state != 'ready':
                        continue
                    content = change.get('after')
                    if not isinstance(content, str) or not content.strip() or len(content) > 48000:
                        raise AppError('OPENVIKING_RESPONSE_INVALID', '记忆正文无效', 502)
                    digest = hashlib.sha256(content.encode()).hexdigest()
                    if item is None:
                        item = AutomaticMemory(user_id=job.user_id, generation=job.generation, uri=uri,
                            content=content, content_hash=digest, source_session_id=UUID(job.receipt['session_id']),
                            source_message_id=UUID(job.receipt['source_message_id']))
                        transaction.save(item)
                        rows[uri] = item
                    elif item.content_hash != digest:
                        item.content, item.content_hash, item.version = content, digest, item.version + 1
                        item.source_session_id, item.source_message_id = UUID(job.receipt['session_id']), UUID(job.receipt['source_message_id'])
            current.state, current.lease_until = 'succeeded', None
            transaction.commit()

    def _capture(self, job):
        with transaction_scope(self.database) as transaction:
            consent = dao.settings(transaction, job.user_id)
            content = dao.capture_source(transaction, job)
            if not consent or not consent.enabled or consent.generation != job.generation or content is None:
                self._save_progress(job, job.receipt, 'cancelled')
                return
        receipt = dict(job.receipt)
        remote = 'rag-' + str(job.id)
        uid, generation = job.user_id, job.generation
        phase = receipt.get('phase', 'new')
        if phase in {'appending', 'committing'}:
            # Unknown HTTP outcomes cannot safely be replayed as successful writes.
            self._save_progress(job, receipt, 'uncertain', 'MEMORY_WRITE_UNCERTAIN')
            return
        if phase == 'new':
            existing = self.manager.request(uid, generation, 'GET', f'/api/v1/sessions/{remote}', missing_ok=True, background=True)
            if existing is None:
                self.manager.request(uid, generation, 'POST', '/api/v1/sessions', background=True,
                    json={'session_id': remote, 'memory_policy': {'memory_types': ['profile', 'preferences', 'entities'],
                          'self': {'enabled': True}, 'peer': {'enabled': False}},
                          'auto_commit_policy': {'pending_token_threshold': 1000000,
                              'message_count_threshold': 1000, 'idle_timeout_seconds': 0}})
            receipt['phase'] = 'appending'
            self._save_progress(job, receipt)
            # Only user-authored text is captured. Model answers and tool output are excluded.
            self.manager.request(uid, generation, 'POST', f'/api/v1/sessions/{remote}/messages',
                                 background=True, json={'role': 'user', 'content': content})
            receipt['phase'] = 'appended'
            self._save_progress(job, receipt)
            phase = 'appended'
        if phase == 'appended':
            receipt['phase'] = 'committing'
            self._save_progress(job, receipt)
            commit = self.manager.request(uid, generation, 'POST', f'/api/v1/sessions/{remote}/commit', background=True, json={})
            if not isinstance(commit, dict):
                raise AppError('OPENVIKING_RESPONSE_INVALID', '记忆提交返回无效结果', 502)
            receipt.update(phase='waiting', task_id=commit.get('task_id'), archive_uri=commit.get('archive_uri'))
            self._save_progress(job, receipt)
            if not receipt['task_id']:
                if commit.get('status') != 'skipped':
                    raise AppError('OPENVIKING_RESPONSE_INVALID', '记忆提交缺少任务标识', 502)
                self._publish(job, {})
                return
        task = self.manager.request(uid, generation, 'GET', f"/api/v1/tasks/{receipt['task_id']}", background=True)
        if not isinstance(task, dict) or task.get('status') not in {'pending', 'running', 'cancelling', 'completed', 'failed', 'cancelled'}:
            raise AppError('OPENVIKING_RESPONSE_INVALID', '记忆任务状态无效', 502)
        if task['status'] in {'failed', 'cancelled'}:
            raise AppError('MEMORY_EXTRACTION_FAILED', '后台记忆提取失败', 502)
        if task.get('status') != 'completed':
            if (utcnow() - aware(job.created_at)).total_seconds() > self.settings.openviking_task_timeout:
                raise AppError('MEMORY_EXTRACTION_TIMEOUT', '后台记忆提取超时', 504)
            self._save_progress(job, receipt, 'waiting')
            return
        result = task.get('result') or {}
        if not isinstance(result, dict):
            raise AppError('OPENVIKING_RESPONSE_INVALID', '记忆任务结果无效', 502)
        archive = result.get('archive_uri') or receipt.get('archive_uri')
        operations = self.manager.read_diff(uid, generation, archive)
        with transaction_scope(self.database) as transaction:
            tombstones = [m.uri for m in dao.memories(transaction, uid, generation, include_deleted=True) if m.state != 'ready']
        for uri in tombstones:
            self.manager.remove(uid, generation, uri)
        self._publish(job, operations)

    def _cleanup(self, job):
        # Generation fencing protects recall immediately. Physical deletion also
        # waits for earlier remote writes, otherwise extraction can recreate files.
        with transaction_scope(self.database) as transaction:
            captures = dao.captures(transaction, job.user_id, job.generation)
        for capture in captures:
            receipt = capture.receipt
            if receipt.get('phase', 'new') == 'new' or receipt.get('source_removed'):
                continue
            if receipt.get('phase') == 'committing' and not receipt.get('task_id'):
                # A timed-out commit may still run remotely. Never certify deletion
                # without its task receipt; an operator must reconcile this case.
                raise AppError('MEMORY_CLEANUP_UNCERTAIN', '外部提交结果待核查，尚不能确认清理完成', 409)
            if receipt.get('task_id'):
                task = self.manager.request(job.user_id, job.generation, 'GET',
                    f"/api/v1/tasks/{receipt['task_id']}", background=True)
                if not isinstance(task, dict) or task.get('status') not in {
                        'pending', 'running', 'cancelling', 'completed', 'failed', 'cancelled'}:
                    raise AppError('OPENVIKING_RESPONSE_INVALID', '清理前无法核验记忆任务状态', 502)
                if task['status'] not in {'completed', 'failed', 'cancelled'}:
                    if (utcnow() - aware(job.created_at)).total_seconds() > self.settings.openviking_task_timeout:
                        raise AppError('MEMORY_CLEANUP_TIMEOUT', '等待外部提取结束超时，清理尚未完成', 504)
                    self._save_progress(job, job.receipt, 'waiting')
                    return
            self._save_progress(job, job.receipt)
            self.manager.remove_session(job.user_id, job.generation, capture.id)
            with transaction_scope(self.database) as transaction:
                dao.owned(transaction, job)
                dao.mark_source_removed(transaction, capture)
                transaction.commit()
        self._save_progress(job, job.receipt)
        self.manager.remove(job.user_id, job.generation, job.receipt.get('uri'))
        with transaction_scope(self.database) as transaction:
            current = dao.owned(transaction, job)
            for item in dao.memories(transaction, job.user_id, job.generation, include_deleted=True):
                if job.kind == 'purge' or item.uri == job.receipt['uri']:
                    item.state, item.content = 'deleted', ''
            current.state, current.lease_until = 'succeeded', None
            transaction.commit()

    def run_once(self):
        self.require()
        with transaction_scope(self.database) as transaction:
            job = dao.claim(transaction)
            transaction.commit()
        if job is None:
            return False
        try:
            if job.kind == 'capture':
                self._capture(job)
            else:
                self._cleanup(job)
        except AppError as error:
            if error.code == 'MEMORY_LEASE_LOST':
                return True
            with transaction_scope(self.database) as transaction:
                current = dao.owned(transaction, job)
                current.attempts += 1
                uncertain = current.receipt.get('phase') in {'appending', 'committing'} or error.code == 'MEMORY_CLEANUP_UNCERTAIN'
                current.state = 'uncertain' if uncertain else 'failed' if current.attempts >= 3 else 'pending'
                current.error_code, current.lease_until = error.code, None
                current.available_at = utcnow() + timedelta(seconds=2 ** current.attempts)
                transaction.commit()
            logger.warning('automatic_memory_sync_failed', extra={'error_code': error.code, 'resource_id': str(job.id)})
        return True
