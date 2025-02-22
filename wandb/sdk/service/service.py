"""Reliably launch and connect to backend server process (wandb service).

Backend server process can be connected to using tcp sockets transport.
"""
import datetime
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from typing import TYPE_CHECKING, Any, Dict, Optional

from wandb import _sentry, termlog
from wandb.env import error_reporting_enabled
from wandb.errors import Error
from wandb.sdk.lib.wburls import wburls
from wandb.util import get_core_path, get_module

from . import _startup_debug, port_file
from .service_base import ServiceInterface
from .service_sock import ServiceSockInterface

if TYPE_CHECKING:
    from wandb.sdk.wandb_settings import Settings


class ServiceStartProcessError(Error):
    """Raised when a known error occurs when launching wandb service."""

    pass


class ServiceStartTimeoutError(Error):
    """Raised when service start times out."""

    pass


class ServiceStartPortError(Error):
    """Raised when service start fails to find a port."""

    pass


class _Service:
    _settings: "Settings"
    _sock_port: Optional[int]
    _service_interface: ServiceInterface
    _internal_proc: Optional[subprocess.Popen]
    _startup_debug_enabled: bool

    def __init__(
        self,
        settings: "Settings",
    ) -> None:
        self._settings = settings
        self._stub = None
        self._sock_port = None
        self._internal_proc = None
        self._startup_debug_enabled = _startup_debug.is_enabled()

        _sentry.configure_scope(tags=dict(settings), process_context="service")

        # current code only supports socket server implementation, in the
        # future we might be able to support both
        self._service_interface = ServiceSockInterface()

    def _startup_debug_print(self, message: str) -> None:
        if not self._startup_debug_enabled:
            return
        _startup_debug.print_message(message)

    def _wait_for_ports(
        self, fname: str, proc: Optional[subprocess.Popen] = None
    ) -> None:
        """Wait for the service to write the port file and then read it.

        Args:
            fname: The path to the port file.
            proc: The process to wait for.

        Raises:
            ServiceStartTimeoutError: If the service takes too long to start.
            ServiceStartPortError: If the service writes an invalid port file or unable to read it.
            ServiceStartProcessError: If the service process exits unexpectedly.

        """
        time_max = time.monotonic() + self._settings._service_wait
        while time.monotonic() < time_max:
            if proc and proc.poll():
                # process finished
                # define these variables for sentry context grab:
                # command = proc.args
                # sys_executable = sys.executable
                # which_python = shutil.which("python3")
                # proc_out = proc.stdout.read()
                # proc_err = proc.stderr.read()
                context = dict(
                    command=proc.args,
                    sys_executable=sys.executable,
                    which_python=shutil.which("python3"),
                    proc_out=proc.stdout.read() if proc.stdout else "",
                    proc_err=proc.stderr.read() if proc.stderr else "",
                )
                raise ServiceStartProcessError(
                    f"The wandb service process exited with {proc.returncode}. "
                    "Ensure that `sys.executable` is a valid python interpreter. "
                    "You can override it with the `_executable` setting "
                    "or with the `WANDB__EXECUTABLE` environment variable.",
                    context=context,
                )
            if not os.path.isfile(fname):
                time.sleep(0.2)
                continue
            try:
                pf = port_file.PortFile()
                pf.read(fname)
                if not pf.is_valid:
                    time.sleep(0.2)
                    continue
                self._sock_port = pf.sock_port
            except Exception as e:
                # todo: point at the docs. this could be due to a number of reasons,
                #  for example, being unable to write to the port file etc.
                raise ServiceStartPortError(
                    f"Failed to allocate port for wandb service: {e}."
                )
            return
        raise ServiceStartTimeoutError(
            "Timed out waiting for wandb service to start after "
            f"{self._settings._service_wait} seconds. "
            "Try increasing the timeout with the `_service_wait` setting."
        )

    def _launch_server(self) -> None:
        """Launch server and set ports."""
        # References for starting processes
        # - https://github.com/wandb/wandb/blob/archive/old-cli/wandb/__init__.py
        # - https://stackoverflow.com/questions/1196074/how-to-start-a-background-process-in-python
        self._startup_debug_print("launch")

        kwargs: Dict[str, Any] = dict(close_fds=True)
        # flags to handle keyboard interrupt signal that is causing a hang
        if platform.system() == "Windows":
            kwargs.update(creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)  # type: ignore [attr-defined]
        else:
            kwargs.update(start_new_session=True)

        pid = str(os.getpid())

        with tempfile.TemporaryDirectory() as tmpdir:
            fname = os.path.join(tmpdir, f"port-{pid}.txt")

            executable = self._settings._executable
            exec_cmd_list = [executable, "-m"]
            # Add coverage collection if needed
            if os.environ.get("YEA_RUN_COVERAGE") and os.environ.get("COVERAGE_RCFILE"):
                exec_cmd_list += ["coverage", "run", "-m"]

            service_args = []
            # NOTE: "wandb-core" is the name of the package that will be distributed
            #       as the stable version of the wandb core library.
            #
            #       Environment variable _WANDB_CORE_PATH is a temporary development feature
            #       to assist in running the core service from a live development directory.
            core_path = get_core_path()
            if core_path:
                service_args.extend([core_path])
                if not error_reporting_enabled():
                    service_args.append("--no-observability")
                exec_cmd_list = []
                # TODO: remove this after the wandb-core GA release
                wandb_core = get_module("wandb_core")
                termlog(
                    f"Using wandb-core version {wandb_core.__version__} as the SDK backend. "
                    f"Please refer to {wburls.get('wandb_core')} for more information.",
                    repeat=False,
                )
            else:
                service_args.extend(["wandb", "service"])

            service_args += [
                "--port-filename",
                fname,
                "--pid",
                pid,
                "--debug",
            ]
            service_args.append("--serve-sock")

            if os.environ.get("WANDB_SERVICE_PROFILE") == "memray":
                _ = get_module(
                    "memray",
                    required=(
                        "wandb service memory profiling requires memray, "
                        "install with `pip install memray`"
                    ),
                )

                time_tag = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
                output_file = f"wandb_service.memray.{time_tag}.bin"
                cli_executable = (
                    pathlib.Path(__file__).parent.parent.parent.parent
                    / "tools"
                    / "cli.py"
                )
                exec_cmd_list = [
                    executable,
                    "-m",
                    "memray",
                    "run",
                    "-o",
                    output_file,
                ]
                service_args[0] = str(cli_executable)
                termlog(
                    f"wandb service memory profiling enabled, output file: {output_file}"
                )
                termlog(
                    f"Convert to flamegraph with: `python -m memray flamegraph {output_file}`"
                )

            try:
                internal_proc = subprocess.Popen(
                    exec_cmd_list + service_args,
                    env=os.environ,
                    **kwargs,
                )
            except Exception as e:
                _sentry.reraise(e)

            self._startup_debug_print("wait_ports")
            try:
                self._wait_for_ports(fname, proc=internal_proc)
            except Exception as e:
                _sentry.reraise(e)
            self._startup_debug_print("wait_ports_done")
            self._internal_proc = internal_proc
        self._startup_debug_print("launch_done")

    def start(self) -> None:
        self._launch_server()

    @property
    def sock_port(self) -> Optional[int]:
        return self._sock_port

    @property
    def service_interface(self) -> ServiceInterface:
        return self._service_interface

    def join(self) -> int:
        ret = 0
        if self._internal_proc:
            ret = self._internal_proc.wait()
        return ret
