"""Running a shell command so that stopping it stops everything it started.

subprocess.run(timeout=...) only kills the direct child: a dashboard command
like `source setup.bash; ros2 topic pub -r 10 /cmd_vel ...` (or a Bash BT
node doing the same) kept publishing after "[killed - exceeded 30s
timeout]". Here the command gets its own process group, and a timeout or a
stop() ends the whole group.
"""
import os
import signal
import subprocess


class GroupProcess:
    def __init__(self, args, **popen_kwargs):
        self.proc = subprocess.Popen(args, start_new_session=True, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, **popen_kwargs)

    def stop(self, grace_s=2.0):
        """SIGTERM to the whole group, then SIGKILL if it's still there."""
        if self.proc.poll() is not None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        try:
            self.proc.wait(grace_s)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    def run(self, timeout):
        """(returncode, stdout, stderr); raises subprocess.TimeoutExpired
        (with whatever output there was) after stopping the group."""
        try:
            out, err = self.proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.stop()
            out, err = self.proc.communicate()
            raise subprocess.TimeoutExpired(self.proc.args, timeout, output=out, stderr=err)
        return self.proc.returncode, out, err


def run_group(args, timeout, **popen_kwargs):
    return GroupProcess(args, **popen_kwargs).run(timeout)
