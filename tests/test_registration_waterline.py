from __future__ import annotations

import unittest
from unittest.mock import patch

from app.control.account.enums import AccountStatus
from app.control.account.models import AccountRecord, RuntimeSnapshot
from app.control.registration.manager import RegistrationManager


class _SnapshotRepository:
    def __init__(self, records: list[AccountRecord]) -> None:
        self._snapshot = RuntimeSnapshot(items=records)

    async def runtime_snapshot(self) -> RuntimeSnapshot:
        return self._snapshot


class WaterlineAccountCountTests(unittest.IsolatedAsyncioTestCase):
    async def test_counts_only_normal_status_for_normal_and_build_pools(self) -> None:
        repository = _SnapshotRepository([
            AccountRecord(token='normal-active', status=AccountStatus.ACTIVE),
            AccountRecord(token='normal-cooling', status=AccountStatus.COOLING),
            AccountRecord(token='normal-expired', status=AccountStatus.EXPIRED),
            AccountRecord(token='normal-disabled', status=AccountStatus.DISABLED),
            AccountRecord(token='normal-deleted', status=AccountStatus.ACTIVE, deleted_at=1),
        ])
        manager = RegistrationManager(repository)
        build_rows = [
            {'id': 'build-active-no-models', 'status': 'active', 'enabled': True, 'models': []},
            {'id': 'build-cooling', 'status': 'cooling', 'enabled': True, 'models': ['grok-4.5']},
            {'id': 'build-disabled', 'status': 'disabled', 'enabled': True, 'models': ['grok-4.5']},
            {'id': 'build-manual-disabled', 'status': 'manual_disabled', 'enabled': False, 'models': ['grok-4.5']},
        ]
        with patch('app.control.build.accounts.store.list', return_value=build_rows):
            counts = await manager._available_account_counts()

        self.assertEqual(counts, {'normal_available': 1, 'build_available': 1})


if __name__ == '__main__':
    unittest.main()
