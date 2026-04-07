/*
 * restart_coordinator — setuid-root wrapper to restart the voice
 * coordinator LaunchDaemon without an interactive sudo prompt.
 *
 * Build:  cc -o restart_coordinator restart_coordinator.c
 * Install on Daedalus:
 *   sudo cp restart_coordinator /usr/local/bin/
 *   sudo chown root:wheel /usr/local/bin/restart_coordinator
 *   sudo chmod 4755 /usr/local/bin/restart_coordinator
 *
 * Usage:  restart_coordinator
 *
 * macOS ignores setuid on interpreted scripts, so this must be
 * a compiled binary.  The execv'd command is hardcoded — no user
 * input is parsed, so there is no injection surface.
 *
 * Perry Kivolowitz, 2026.
 */

#include <unistd.h>
#include <stdio.h>

int main(void) {
    /* Escalate to real root — setuid gives euid=0 but launchctl
       checks real uid on some operations. */
    if (setuid(0) != 0) {
        perror("setuid");
        return 1;
    }

    char *argv[] = {
        "/bin/launchctl",
        "kickstart",
        "-k",
        "system/com.glowup.coordinator",
        NULL,
    };

    execv("/bin/launchctl", argv);
    perror("execv");
    return 1;
}
