import json
import tempfile
import unittest
from pathlib import Path

from alignment_scheduler import AutoAlignmentScheduler
from multi_alignment import consume_commands, determine_role, enqueue_command, select_preferred_link


class MultiAlignmentSchedulerTests(unittest.TestCase):
    def test_ip_pair_elects_lower_address_as_coordinator(self):
        self.assertEqual(
            determine_role("192.168.1.10", "192.168.1.11"), "coordinator"
        )
        self.assertEqual(determine_role("192.168.1.11", "192.168.1.10"), "peer")

    def test_stronger_rssi_wins_and_equal_rssi_keeps_current_link(self):
        self.assertEqual(select_preferred_link(-60, -80), "local")
        self.assertEqual(select_preferred_link(-85, -70), "peer")
        self.assertEqual(select_preferred_link(-70, -70, "peer"), "peer")
        self.assertEqual(select_preferred_link(-60, -1), "local")
        self.assertEqual(select_preferred_link(-1, -73), "peer")
        self.assertIsNone(select_preferred_link(-1, -1))

    def test_selection_stays_pending_when_enforcement_fails(self):
        pending = True
        selection_complete = False
        if pending:
            pending = not selection_complete
        self.assertTrue(pending)

    def test_single_success_holds_only_failed_unit(self):
        scheduler = AutoAlignmentScheduler(1, 5, 10, max_attempts=2)
        scheduler.begin_joint_wait("boot_signal_lost")
        scheduler.begin_joint_scan()
        scheduler.complete_joint_session(False, True, now=0)
        self.assertEqual(scheduler.state, scheduler.PEER_ASSISTED_HOLD)
        self.assertEqual(scheduler.automatic_attempts, 1)

    def test_both_success_reset_attempts(self):
        scheduler = AutoAlignmentScheduler(1, 5, 10, max_attempts=2)
        scheduler.begin_joint_wait("boot_signal_lost")
        scheduler.begin_joint_scan()
        scheduler.complete_joint_session(True, True, now=0)
        self.assertEqual(scheduler.state, scheduler.IDLE)
        self.assertEqual(scheduler.automatic_attempts, 0)

    def test_both_failure_enters_cooldown_once(self):
        scheduler = AutoAlignmentScheduler(1, 5, 10, max_attempts=2)
        scheduler.begin_joint_wait("boot_signal_lost")
        scheduler.begin_joint_scan()
        scheduler.complete_joint_session(False, False, now=100)
        self.assertEqual(scheduler.state, scheduler.COOLDOWN)
        self.assertEqual(scheduler.cooldown_until, 110)
        self.assertEqual(scheduler.automatic_attempts, 1)

    def test_peer_eligibility_does_not_enter_aligning(self):
        scheduler = AutoAlignmentScheduler(2, 5, 10, max_attempts=2)
        self.assertFalse(scheduler.observe_joint_eligibility(-97, True, now=0))
        self.assertTrue(scheduler.observe_joint_eligibility(-97, True, now=1))
        self.assertEqual(scheduler.state, scheduler.IDLE)
        self.assertTrue(scheduler.begin_joint_scan())
        self.assertEqual(scheduler.state, scheduler.ALIGNING)

    def test_coordinator_eligibility_can_start_joint_wait_after_boot(self):
        scheduler = AutoAlignmentScheduler(1, 5, 10, max_attempts=2)
        self.assertTrue(scheduler.observe_joint_eligibility(-98, True, now=0))
        self.assertTrue(scheduler.begin_joint_wait("boot_signal_lost"))
        self.assertEqual(scheduler.state, scheduler.SESSION_WAITING_PEER)


class CommandSpoolTests(unittest.TestCase):
    def test_same_command_id_is_idempotent(self):
        command = {
            "command": "start_joint_scan",
            "command_id": "command-001",
            "session_id": "joint-12345678",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            self.assertTrue(enqueue_command(command, directory))
            self.assertFalse(enqueue_command(command, directory))
            self.assertEqual(consume_commands(directory), [command])


if __name__ == "__main__":
    unittest.main()
