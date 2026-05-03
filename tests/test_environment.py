"""Environment sanity checks.

Validates system-level prerequisites that can cause subtle failures
when misconfigured.  These tests are intended to be run on deployment
targets (Pi, Jetsons, etc.) as part of a pre-flight check.

Run::

    python3 -m pytest tests/test_environment.py -v
"""

# Copyright (c) 2026 Perry Kivolowitz. All rights reserved.
# Licensed under the MIT License. See LICENSE file in the project root.

__version__ = "1.0"

import os
import platform
import shutil
import subprocess
import unittest


class TestNTP(unittest.TestCase):
    """Verify NTP time synchronization is active.

    A drifted system clock causes silent failures when connecting to
    PostgreSQL (or any service) on a host with a correct clock.
    Authentication protocols and TLS certificate validation can both
    fail with clock skew.
    """

    @unittest.skipUnless(
        platform.system() == "Linux",
        "NTP service check only applies to Linux deployment targets",
    )
    def test_ntp_synchronized(self) -> None:
        """System clock should be synchronized via NTP.

        Checks ``timedatectl`` for NTP synchronization status.  Fails
        with an actionable message if NTP is not active.
        """
        if not shutil.which("timedatectl"):
            self.skipTest("timedatectl not available")

        result: subprocess.CompletedProcess = subprocess.run(
            ["timedatectl", "show", "--property=NTPSynchronized"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        output: str = result.stdout.strip()
        is_synced: bool = "NTPSynchronized=yes" in output

        self.assertTrue(
            is_synced,
            "NTP is NOT synchronized. This causes silent failures when "
            "connecting to PostgreSQL and other network services. "
            "Fix with: sudo timedatectl set-ntp true",
        )

    @unittest.skipUnless(
        platform.system() == "Linux",
        "NTP service check only applies to Linux deployment targets",
    )
    def test_ntp_service_active(self) -> None:
        """An NTP service (timesyncd, chrony, or ntpd) should be running.

        Checks for common NTP daemons.  Fails with an actionable
        message if none are found.
        """
        if not shutil.which("timedatectl"):
            self.skipTest("timedatectl not available")

        result: subprocess.CompletedProcess = subprocess.run(
            ["timedatectl", "show", "--property=NTP"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        output: str = result.stdout.strip()
        ntp_enabled: bool = "NTP=yes" in output

        self.assertTrue(
            ntp_enabled,
            "NTP service is NOT enabled. The system clock will drift "
            "and cause authentication failures with PostgreSQL. "
            "Fix with: sudo timedatectl set-ntp true",
        )


if __name__ == "__main__":
    unittest.main()
