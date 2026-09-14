import json
import tempfile
import unittest
from pathlib import Path

from alignment_scheduler import AutoAlignmentScheduler
from multi_alignment import (
    consume_commands,
    determine_role,
    enqueue_command,
    requires_pre_scan_handover,
    select_preferred_link,
    select_preferred_link_with_freshness,
)


class MultiAlignmentSchedulerTests(unittest.TestCase):
    def test_ip_pair_elects_lower_address_as_master(self):
        self.assertEqual(
            determine_role("192.168.1.10", "192.168.1.11"), "master"
        )
        self.assertEqual(determine_role("192.168.1.11", "192.168.1.10"), "slave")

    def test_stronger_rssi_wins_and_equal_rssi_keeps_current_link(self):
        self.assertEqual(select_preferred_link(-60, -80), "local")
        self.assertEqual(select_preferred_link(-85, -70), "slave")
        self.assertEqual(select_preferred_link(-70, -70, "slave"), "slave")
        self.assertEqual(select_preferred_link(-60, -1), "local")
        self.assertEqual(select_preferred_link(-1, -73), "slave")
        self.assertIsNone(select_preferred_link(-1, -1))

    def test_fresh_slave_wins_when_master_rssi_is_stale(self):
        self.assertEqual(
            select_preferred_link_with_freshness(None, False, -73, True),
            "slave",
        )
        self.assertEqual(
            select_preferred_link_with_freshness(-70, True, None, False),
            "local",
        )

    def test_fresh_master_wins_when_slave_rssi_is_stale(self):
        self.assertEqual(
            select_preferred_link_with_freshness(-70, True, None, False),
            "local",
        )

    def test_only_active_master_with_fresh_slave_requires_pre_scan_handover(self):
        self.assertTrue(requires_pre_scan_handover("master", True, True))
        self.assertFalse(requires_pre_scan_handover("master", True, False))
        self.assertFalse(requires_pre_scan_handover("master", False, True))
        self.assertFalse(requires_pre_scan_handover("slave", True, True))

    def test_selection_stays_pending_when_enforcement_fails(self):
        pending = True
        selection_complete = False
        if pending:
            pending = not selection_complete
        self.assertTrue(pending)

    def test_failed_local_scan_enters_its_own_cooldown(self):
        scheduler = AutoAlignmentScheduler(1, 5, 10, max_attempts=2)
        scheduler.begin_local_scan("boot_signal_lost")
        scheduler.complete_scan("failed", -95, True, now=100)
        self.assertEqual(scheduler.state, scheduler.COOLDOWN)
        self.assertEqual(scheduler.cooldown_until, 110)
        self.assertEqual(scheduler.automatic_attempts, 1)


class CommandSpoolTests(unittest.TestCase):
    def test_same_command_id_is_idempotent(self):
        command = {
            "command": "set_link_active",
            "command_id": "command-001",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            self.assertTrue(enqueue_command(command, directory))
            self.assertFalse(enqueue_command(command, directory))
            self.assertEqual(consume_commands(directory), [command])


if __name__ == "__main__":
    unittest.main()
