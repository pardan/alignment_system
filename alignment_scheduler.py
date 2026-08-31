"""Deterministic policy for automatic alignment requests.

This module deliberately has no GPIO, SNMP, or systemd dependency so its state
transitions can be tested independently of the Raspberry Pi hardware.
"""


class AutoAlignmentScheduler:
    WAITING_FOR_RSSI = "waiting_for_rssi"
    IDLE = "idle"
    ALIGNING = "aligning"
    COOLDOWN = "cooldown"
    SESSION_WAITING_PEER = "session_waiting_peer"
    PEER_ASSISTED_HOLD = "peer_assisted_hold"

    def __init__(self, boot_minus_one_count, signal_loss_duration_sec,
                 cooldown_sec, max_attempts=None, signal_loss_rssi_threshold=-90):
        self.boot_minus_one_count = boot_minus_one_count
        self.signal_loss_duration_sec = signal_loss_duration_sec
        self.cooldown_sec = cooldown_sec
        self.max_attempts = max_attempts
        self.signal_loss_rssi_threshold = signal_loss_rssi_threshold
        self.state = self.WAITING_FOR_RSSI
        self.has_seen_normal_rssi = False
        self.boot_minus_one_reads = 0
        self.signal_loss_started_at = None
        self.cooldown_until = None
        self.automatic_attempts = 0
        self.recovery_ready = False

    def _automatic_attempt_allowed(self):
        return self.max_attempts is None or self.automatic_attempts < self.max_attempts

    def is_signal_lost(self, rssi):
        """-1 and values below the configured threshold both mean signal loss."""
        return rssi == -1 or (
            rssi is not None and rssi < self.signal_loss_rssi_threshold
        )

    def _start_scan(self, reason):
        self.state = self.ALIGNING
        if reason != "manual_button":
            self.automatic_attempts += 1
        return reason

    def _reset_after_normal_signal(self):
        self.has_seen_normal_rssi = True
        self.boot_minus_one_reads = 0
        self.signal_loss_started_at = None
        self.cooldown_until = None
        self.automatic_attempts = 0
        self.recovery_ready = False
        self.state = self.IDLE

    def observe_rssi(self, rssi, is_fresh, now):
        """Consume one RSSI observation and return an automatic scan reason or None."""
        if not is_fresh or rssi is None:
            return None
        if self.state == self.ALIGNING:
            return None
        if not self.is_signal_lost(rssi):
            self._reset_after_normal_signal()
            return None

        if self.state == self.WAITING_FOR_RSSI:
            self.boot_minus_one_reads += 1
            if self.boot_minus_one_reads >= self.boot_minus_one_count:
                if self._automatic_attempt_allowed():
                    return self._start_scan("boot_signal_lost")
                self.state = self.IDLE
            return None

        if self.state == self.COOLDOWN:
            if now < self.cooldown_until:
                return None
            self.state = self.IDLE
            if self.recovery_ready:
                if self._automatic_attempt_allowed():
                    return self._start_scan("signal_loss")
                return None

        if not self.has_seen_normal_rssi:
            return None
        if self.signal_loss_started_at is None:
            self.signal_loss_started_at = now
            return None
        if now - self.signal_loss_started_at < self.signal_loss_duration_sec:
            return None
        if not self._automatic_attempt_allowed():
            return None
        return self._start_scan("signal_loss")

    def observe_joint_eligibility(self, rssi, is_fresh, now):
        """Track automatic joint-scan eligibility without entering ALIGNING.

        A peer must advertise its loss state to the coordinator while remaining
        ready to receive ``start_joint_scan``.  Calling ``observe_rssi`` would
        transition it to ALIGNING before that command arrives.
        """
        if not is_fresh or rssi is None:
            return False
        if self.state in (
            self.ALIGNING,
            self.SESSION_WAITING_PEER,
            self.PEER_ASSISTED_HOLD,
        ):
            return False
        if not self.is_signal_lost(rssi):
            self._reset_after_normal_signal()
            return False
        if self.state == self.WAITING_FOR_RSSI:
            self.boot_minus_one_reads += 1
            if self.boot_minus_one_reads >= self.boot_minus_one_count:
                self.state = self.IDLE
                return self._automatic_attempt_allowed()
            return False
        if self.state == self.COOLDOWN:
            if now < self.cooldown_until:
                return False
            self.state = self.IDLE
            return self.recovery_ready and self._automatic_attempt_allowed()
        if not self.has_seen_normal_rssi and (
            self.boot_minus_one_reads >= self.boot_minus_one_count
        ):
            return self._automatic_attempt_allowed()
        if not self.has_seen_normal_rssi:
            return False
        if self.signal_loss_started_at is None:
            self.signal_loss_started_at = now
            return False
        return (
            now - self.signal_loss_started_at >= self.signal_loss_duration_sec
            and self._automatic_attempt_allowed()
        )

    def request_manual_scan(self):
        """Return the manual reason when the Start button is allowed to launch a scan."""
        if self.state == self.ALIGNING:
            return None
        if self.state in (self.SESSION_WAITING_PEER, self.PEER_ASSISTED_HOLD):
            return None
        if self.state not in (self.IDLE, self.COOLDOWN):
            return None
        return self._start_scan("manual_button")

    def complete_scan(self, outcome, rssi, is_fresh, now):
        """Apply final scan outcome and return whether the scheduler entered cooldown."""
        if outcome == "signal_recovered" or (
            is_fresh and rssi is not None and not self.is_signal_lost(rssi)
        ):
            self._reset_after_normal_signal()
            return False
        self.state = self.COOLDOWN
        self.cooldown_until = now + self.cooldown_sec
        self.recovery_ready = True
        return True

    def begin_joint_wait(self, reason="joint_scan"):
        """Record a coordinator-created session before its local scan starts."""
        if self.state == self.ALIGNING:
            return False
        if reason != "manual_button":
            if not self._automatic_attempt_allowed():
                return False
            self.automatic_attempts += 1
        self.state = self.SESSION_WAITING_PEER
        return True

    def begin_joint_scan(self, reason="joint_scan"):
        """Enter one locally-executing joint scan without double-counting retries."""
        if self.state == self.ALIGNING:
            return False
        self.state = self.ALIGNING
        return True

    def enter_peer_assisted_hold(self):
        """Hold a failed peer until a future coordinator-authorized joint session."""
        self.signal_loss_started_at = None
        self.cooldown_until = None
        self.recovery_ready = False
        self.state = self.PEER_ASSISTED_HOLD

    def complete_joint_session(self, local_success, peer_success, now):
        """Apply the shared session outcome after both controllers report.

        Automatic attempts are counted once by the coordinator before the joint
        scan begins.  Therefore a one-success result must not consume an extra
        retry/cooldown on the held unit.
        """
        if local_success and peer_success:
            self._reset_after_normal_signal()
            return self.state
        if local_success != peer_success:
            if not local_success:
                self.enter_peer_assisted_hold()
            else:
                self.state = self.IDLE
                self.signal_loss_started_at = None
                self.cooldown_until = None
                self.recovery_ready = False
            return self.state
        self.state = self.COOLDOWN
        self.cooldown_until = now + self.cooldown_sec
        self.recovery_ready = True
        return self.state
