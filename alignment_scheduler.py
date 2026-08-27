"""Deterministic policy for automatic alignment requests.

This module deliberately has no GPIO, SNMP, or systemd dependency so its state
transitions can be tested independently of the Raspberry Pi hardware.
"""


class AutoAlignmentScheduler:
    WAITING_FOR_RSSI = "waiting_for_rssi"
    IDLE = "idle"
    ALIGNING = "aligning"
    COOLDOWN = "cooldown"

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
        return rssi == -1 or rssi < self.signal_loss_rssi_threshold

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

    def request_manual_scan(self):
        """Return the manual reason when the Start button is allowed to launch a scan."""
        if self.state == self.ALIGNING:
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
