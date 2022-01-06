# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
import time
from typing import List, Type

from assertpy import assert_that

from lisa.executable import Tool
from lisa.operating_system import Debian, Fedora
from lisa.tools import Gcc, Git, Make, Modprobe
from lisa.util import UnsupportedDistroException


class DpdkVpp(Tool):

    VPP_SRC_LINK = "https://github.com/FDio/vpp.git"
    REPO_DIR = "vpp"

    @property
    def command(self) -> str:
        return "vpp"

    @property
    def dependencies(self) -> List[Type[Tool]]:
        return [Gcc, Make, Git]

    def start(self) -> None:
        node = self.node
        modprobe = node.tools[Modprobe]
        if isinstance(node.os, Fedora):
            # Fedora/RHEL has strict selinux by default,
            # this messes with the default vpp settings.
            # quick fix is setting permissive mode
            node.execute(
                "setenforce Permissive",
                sudo=True,
                expected_exit_code=0,
                expected_exit_code_failure_message=(
                    "Could not set selinux to permissive"
                ),
            )

        if isinstance(node.os, Debian) or isinstance(node.os, Fedora):
            # It is possible the service has already been started, so
            # rather than assume anything we'll call restart
            # this will force the reload if it's already started
            # or start it if it hasn't started yet.
            modprobe.load("uio_hv_generic")
            node.execute(
                "service vpp restart",
                sudo=True,
                expected_exit_code=0,
                expected_exit_code_failure_message=(
                    "Could not start/restart vpp service"
                ),
            )
        else:
            raise UnsupportedDistroException(
                os=node.os,
                message=("VPP start is not implemented for this platform"),
            )
        time.sleep(3)  # give it a moment to start up

    def run_test(self) -> None:
        node = self.node
        vpp_interface_output = node.execute(
            "vppctl show int",
            sudo=True,
            expected_exit_code=0,
            expected_exit_code_failure_message=(
                "VPP returned error code while gathering interface info"
            ),
        ).stdout
        vpp_detected_interface = (
            "GigabitEthernet" in vpp_interface_output
            or "VirtualFunctionEthernet" in vpp_interface_output
        )
        assert_that(vpp_detected_interface).described_as(
            "VPP did not detect the dpdk VF or Gigabit network interface"
        ).is_true()

    def _install(self) -> bool:
        node = self.node
        if isinstance(node.os, Debian):
            pkg_type = "deb"
        elif isinstance(node.os, Fedora):
            node.os.install_epel()
            pkg_type = "rpm"
        else:
            raise UnsupportedDistroException(self.node.os)

        node.execute(
            (
                "curl -s https://packagecloud.io/install/repositories/fdio/release/"
                f"script.{pkg_type}.sh | sudo bash"
            ),
            shell=True,
            expected_exit_code=0,
            expected_exit_code_failure_message=(
                "Could not install vpp with fdio provided installer"
            ),
        )
        node.os.update_packages("")
        self._install_from_package_manager()
        return True

    def _install_from_package_manager(self) -> None:
        node = self.node
        vpp_packages = ["vpp"]

        if isinstance(node.os, Debian):
            vpp_packages += ["vpp-plugin-dpdk", "vpp-plugin-core"]
        elif isinstance(node.os, Fedora):
            vpp_packages.append("vpp-plugins")
        else:
            raise UnsupportedDistroException(self.node.os)

        node.os.install_packages(list(vpp_packages))
