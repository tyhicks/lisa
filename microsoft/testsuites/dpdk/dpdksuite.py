# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import re
import time
from collections import deque
from typing import Any, Dict, List, Tuple

from assertpy import assert_that, fail

from lisa import (
    Environment,
    LisaException,
    Logger,
    Node,
    RemoteNode,
    SkippedException,
    TestCaseMetadata,
    TestSuite,
    TestSuiteMetadata,
    constants,
)
from lisa.features import NetworkInterface, Sriov
from lisa.nic import NicInfo, Nics
from lisa.testsuite import simple_requirement
from lisa.tools import Dmesg, Echo, Git, Ip, Lspci, Make, Mount
from lisa.util import perf_timer
from lisa.util.parallel import Task, TaskManager
from microsoft.testsuites.dpdk.dpdktestpmd import DpdkTestpmd
from microsoft.testsuites.dpdk.dpdkvpp import DpdkVpp

VDEV_TYPE = "net_vdev_netvsc"
MAX_RING_PING_LIMIT_NS = 200000
DPDK_STABLE_GIT = "http://dpdk.org/git/dpdk-stable"
DPDK_VF_REMOVAL_MAX_TEST_TIME = 60 * 10


@TestSuiteMetadata(
    area="dpdk",
    category="functional",
    description="""
    This test suite check DPDK functionality
    """,
)
class Dpdk(TestSuite):

    # regex for parsing ring ping output for the final line,
    # grabbing the max latency of 99.999% of data in nanoseconds.
    # ex: percentile 99.999 = 12302
    _ring_ping_percentile_regex = re.compile(r"percentile 99.999 = ([0-9]+)")

    @TestCaseMetadata(
        description="""
            netvsc direct pmd version.
            This test case checks DPDK can be built and installed correctly.
            Prerequisites, accelerated networking must be enabled.
            The VM should have at least two network interfaces,
             with one interface for management.
            More detailes refer https://docs.microsoft.com/en-us/azure/virtual-network/setup-dpdk#prerequisites # noqa: E501
        """,
        priority=2,
        requirement=simple_requirement(
            min_nic_count=2,
            network_interface=Sriov(),
        ),
    )
    def verify_dpdk_build_netvsc(
        self, node: Node, log: Logger, variables: Dict[str, Any]
    ) -> None:
        self._verify_dpdk_build(node, log, variables, "netvsc")

    @TestCaseMetadata(
        description="""
            failsafe (azure default, recommended) version.
            This test case checks DPDK can be built and installed correctly.
            Prerequisites, accelerated networking must be enabled.
            The VM should have at least two network interfaces,
            with one interface for management.
            More details: https://docs.microsoft.com/en-us/azure/virtual-network/setup-dpdk#prerequisites # noqa: E501
        """,
        priority=2,
        requirement=simple_requirement(
            min_nic_count=2,
            network_interface=Sriov(),
        ),
    )
    def verify_dpdk_build_failsafe(
        self, node: Node, log: Logger, variables: Dict[str, Any]
    ) -> None:
        self._verify_dpdk_build(node, log, variables, "failsafe")

    @TestCaseMetadata(
        description="""
            test sriov failsafe during vf revoke (receive side)
        """,
        priority=2,
        requirement=simple_requirement(
            min_nic_count=2,
            network_interface=Sriov(),
            min_count=2,
        ),
    )
    def verify_dpdk_sriov_rescind_failover_receiver(
        self, environment: Environment, log: Logger, variables: Dict[str, Any]
    ) -> None:

        test_kits = _init_nodes_concurrent(environment, log, variables, "failsafe")
        sender, receiver = test_kits

        kit_cmd_pairs = generate_send_receive_run_info("failsafe", sender, receiver)

        _run_testpmd_concurrent(
            kit_cmd_pairs, DPDK_VF_REMOVAL_MAX_TEST_TIME, log, rescind_sriov=True
        )

        rescind_tx_pps_set = receiver.testpmd.get_rx_pps_sriov_rescind()
        self._check_rx_or_tx_pps_sriov_rescind("RX", rescind_tx_pps_set)

    @TestCaseMetadata(
        description="""
            test sriov failsafe during vf revoke (send only version)
        """,
        priority=2,
        requirement=simple_requirement(
            min_nic_count=2,
            network_interface=Sriov(),
        ),
    )
    def verify_dpdk_sriov_rescind_failover_send_only(
        self, node: Node, log: Logger, variables: Dict[str, Any]
    ) -> None:

        test_kit = initialize_node_resources(node, log, variables, "failsafe")
        node_nic_info, testpmd = test_kit.node_nic_info, test_kit.testpmd
        test_nic = node_nic_info.get_test_nic()
        testpmd_cmd = testpmd.generate_testpmd_command(
            test_nic, 0, "txonly", "failsafe"
        )
        kit_cmd_pairs = {
            test_kit: testpmd_cmd,
        }

        _run_testpmd_concurrent(
            kit_cmd_pairs, DPDK_VF_REMOVAL_MAX_TEST_TIME, log, rescind_sriov=True
        )

        rescind_tx_pps_set = testpmd.get_tx_pps_sriov_rescind()
        self._check_rx_or_tx_pps_sriov_rescind("TX", rescind_tx_pps_set)

    def _check_rx_or_tx_pps_sriov_rescind(
        self, tx_or_rx: str, pps: Tuple[int, int, int]
    ) -> None:
        before_rescind, during_rescind, after_reenable = pps
        self._check_rx_or_tx_pps(tx_or_rx, before_rescind, sriov_enabled=True)
        self._check_rx_or_tx_pps(tx_or_rx, during_rescind, sriov_enabled=False)
        self._check_rx_or_tx_pps(tx_or_rx, after_reenable, sriov_enabled=True)

    def _check_rx_or_tx_pps(
        self, tx_or_rx: str, pps: int, sriov_enabled: bool = True
    ) -> None:
        if sriov_enabled:
            assert_that(pps).described_as(
                f"{tx_or_rx}-PPS ({pps}) should have been greater "
                "than 2^20 (~1m) PPS before sriov disable."
            ).is_greater_than(2 ** 20)
        else:
            assert_that(pps).described_as(
                f"{tx_or_rx}-PPS ({pps}) should have been less "
                "than 2^20 (~1m) PPS after sriov disable."
            ).is_less_than(2 ** 20)

    @TestCaseMetadata(
        description="""
            verify vpp is able to detect azure network interfaces
            1. run fd.io vpp install scripts
            2. install vpp from their repositories
            3. start vpp service
            4. check that azure interfaces are detected by vpp
        """,
        priority=2,
        requirement=simple_requirement(
            min_nic_count=2,
            network_interface=Sriov(),
        ),
    )
    def verify_dpdk_vpp(
        self, node: Node, log: Logger, variables: Dict[str, Any]
    ) -> None:

        vpp = DpdkVpp(node)
        vpp.install()

        net = Nics(node)
        net.initialize()
        nic = net.get_test_nic()

        # set devices to down and restart vpp service
        ip = node.tools[Ip]
        for dev in [nic.lower, nic.upper]:
            ip.down(dev)
        for dev in [nic.lower, nic.upper]:
            ip.addr_flush(dev)

        vpp.start()
        vpp.run_test()

    def _verify_dpdk_build(
        self,
        node: Node,
        log: Logger,
        variables: Dict[str, Any],
        pmd: str,
    ) -> None:
        # setup and unwrap the resources for this test
        test_kit = initialize_node_resources(node, log, variables, pmd)
        node_nic_info, testpmd = test_kit.node_nic_info, test_kit.testpmd

        # grab a nic and run testpmd
        test_nic = node_nic_info.get_test_nic()

        testpmd_cmd = testpmd.generate_testpmd_command(
            test_nic,
            0,
            "txonly",
            pmd,
        )
        testpmd.run_for_n_seconds(testpmd_cmd, 10)
        tx_pps = testpmd.get_tx_pps()
        log.info(
            f"TX-PPS:{tx_pps} from {test_nic.upper}/{test_nic.lower}:"
            + f"{test_nic.pci_slot}"
        )
        assert_that(tx_pps).described_as(
            f"TX-PPS ({tx_pps}) should have been greater than 2^20 (~1m) PPS."
        ).is_greater_than(2 ** 20)

    @TestCaseMetadata(
        description="""
            This test runs the dpdk ring ping utility from:
            https://github.com/shemminger/dpdk-ring-ping
            to measure the maximum latency for 99.999 percent of packets during
            the test run. The maximum should be under 200000 nanoseconds
            (.2 milliseconds).
            Not dependent on any specific PMD.
        """,
        priority=2,
        requirement=simple_requirement(
            network_interface=Sriov(),
        ),
    )
    def verify_dpdk_ring_ping(
        self, node: Node, log: Logger, variables: Dict[str, Any]
    ) -> None:
        # setup and unwrap the resources for this test
        test_kit = initialize_node_resources(node, log, variables, "failsafe")
        testpmd = test_kit.testpmd

        # grab a nic and run testpmd
        git = node.tools[Git]
        make = node.tools[Make]
        echo = node.tools[Echo]
        rping_build_env_vars = [
            "export RTE_TARGET=build",
            f"export RTE_SDK={str(testpmd.dpdk_path)}",
        ]
        echo.write_to_file(
            ";".join(rping_build_env_vars), node.get_pure_path("~/.bashrc"), append=True
        )
        git_path = git.clone(
            "https://github.com/shemminger/dpdk-ring-ping.git", cwd=node.working_path
        )
        make.run(
            shell=True,
            cwd=git_path,
            expected_exit_code=0,
            expected_exit_code_failure_message="make could not build rping project.",
        ).assert_exit_code()
        # run ringping for 30 seconds
        runcmd = "./build/rping -c 0x03 -n 2 --no-pci --no-huge -- -d 5 -t 10"
        result = node.execute(
            runcmd,
            shell=True,
            cwd=git_path,
            expected_exit_code=0,
            expected_exit_code_failure_message="rping program failed to run correctly.",
        )
        result.assert_exit_code()
        # get the max latency for 99.999 percent of enqueued 'packets'.
        result_regex = self._ring_ping_percentile_regex.search(result.stdout)
        if result_regex and len(result_regex.groups()) == 1:
            max_ping_measured = int(result_regex.group(1))
            assert_that(max_ping_measured).described_as(
                (
                    f"RingPing measured {max_ping_measured} as maximum ping latency,"
                    f" maximum should be less than {MAX_RING_PING_LIMIT_NS}"
                )
            ).is_less_than(MAX_RING_PING_LIMIT_NS)
        else:
            fail(
                (
                    "Could not get latency data from rping result. "
                    f"Search was for 'percentile 99.999 = ([0-9]+)'\n{result.stdout}\n"
                )
            )

    def _verify_dpdk_send_receive_multi_txrx_queue(
        self, environment: Environment, log: Logger, variables: Dict[str, Any], pmd: str
    ) -> None:

        test_kits = _init_nodes_concurrent(environment, log, variables, pmd)
        sender, receiver = test_kits

        kit_cmd_pairs = generate_send_receive_run_info(
            pmd, sender, receiver, txq=16, rxq=16
        )

        results = _run_testpmd_concurrent(kit_cmd_pairs, 15, log)

        # helpful to have the outputs labeled
        log.debug(f"\nSENDER:\n{results[sender]}")
        log.debug(f"\nRECEIVER:\n{results[receiver]}")

        rcv_rx_pps = receiver.testpmd.get_rx_pps()
        snd_tx_pps = sender.testpmd.get_tx_pps()
        log.info(f"receiver rx-pps: {rcv_rx_pps}")
        log.info(f"sender tx-pps: {snd_tx_pps}")

        # differences in NIC type throughput can lead to different snd/rcv counts
        # check that throughput it greater than 1m pps as a baseline
        assert_that(rcv_rx_pps).described_as(
            "Throughput for RECEIVE was below the correct order-of-magnitude"
        ).is_greater_than(2 ** 20)
        assert_that(snd_tx_pps).described_as(
            "Throughput for SEND was below the correct order of magnitude"
        ).is_greater_than(2 ** 20)

    @TestCaseMetadata(
        description="""
            Tests a basic sender/receiver setup for default failsafe driver setup.
            Sender sends the packets, receiver receives them.
            We check both to make sure the received traffic is within the expected
            order-of-magnitude.
        """,
        priority=2,
        requirement=simple_requirement(
            min_nic_count=2,
            network_interface=Sriov(),
            min_count=2,
        ),
    )
    def verify_dpdk_send_receive_multi_txrx_queue_failsafe(
        self, environment: Environment, log: Logger, variables: Dict[str, Any]
    ) -> None:
        self._verify_dpdk_send_receive_multi_txrx_queue(
            environment, log, variables, "failsafe"
        )

    @TestCaseMetadata(
        description="""
            Tests a basic sender/receiver setup for default failsafe driver setup.
            Sender sends the packets, receiver receives them.
            We check both to make sure the received traffic is within the expected
            order-of-magnitude.
        """,
        priority=2,
        requirement=simple_requirement(
            min_nic_count=2,
            network_interface=Sriov(),
            min_count=2,
        ),
    )
    def verify_dpdk_send_receive_multi_txrx_queue_netvsc(
        self, environment: Environment, log: Logger, variables: Dict[str, Any]
    ) -> None:
        self._verify_dpdk_send_receive_multi_txrx_queue(
            environment, log, variables, "netvsc"
        )

    @TestCaseMetadata(
        description="""
            Tests a basic sender/receiver setup for default failsafe driver setup.
            Sender sends the packets, receiver receives them.
            We check both to make sure the received traffic is within the expected
            order-of-magnitude.
        """,
        priority=2,
        requirement=simple_requirement(
            min_nic_count=2,
            network_interface=Sriov(),
            min_count=2,
        ),
    )
    def verify_dpdk_send_receive_failsafe(
        self, environment: Environment, log: Logger, variables: Dict[str, Any]
    ) -> None:
        self._verify_dpdk_send_receive(environment, log, variables, "failsafe")

    @TestCaseMetadata(
        description="""
            Tests a basic sender/receiver setup for direct netvsc pmd setup.
            Sender sends the packets, receiver receives them.
            We check both to make sure the received traffic is within the expected
            order-of-magnitude.
        """,
        priority=2,
        requirement=simple_requirement(
            min_nic_count=2,
            network_interface=Sriov(),
            min_count=2,
        ),
    )
    def verify_dpdk_send_receive_netvsc(
        self, environment: Environment, log: Logger, variables: Dict[str, Any]
    ) -> None:
        self._verify_dpdk_send_receive(environment, log, variables, "netvsc")

    def _verify_dpdk_send_receive(
        self,
        environment: Environment,
        log: Logger,
        variables: Dict[str, Any],
        pmd: str,
    ) -> None:

        # helpful to have the public ips labeled for debugging
        external_ips = []
        for node in environment.nodes.list():
            if isinstance(node, RemoteNode):
                external_ips += node.connection_info[
                    constants.ENVIRONMENTS_NODES_REMOTE_ADDRESS
                ]
            else:
                raise SkippedException()
        log.debug((f"\nsender:{external_ips[0]}\nreceiver:{external_ips[1]}\n"))

        test_kits = _init_nodes_concurrent(environment, log, variables, pmd)
        sender, receiver = test_kits
        kit_cmd_pairs = generate_send_receive_run_info(pmd, sender, receiver)

        results = _run_testpmd_concurrent(kit_cmd_pairs, 15, log)

        # helpful to have the outputs labeled
        log.debug(f"\nSENDER:\n{results[sender]}")
        log.debug(f"\nRECEIVER:\n{results[receiver]}")

        rcv_rx_pps = receiver.testpmd.get_rx_pps()
        snd_tx_pps = sender.testpmd.get_tx_pps()
        log.info(f"receiver rx-pps: {rcv_rx_pps}")
        log.info(f"sender tx-pps: {snd_tx_pps}")

        # differences in NIC type throughput can lead to different snd/rcv counts
        assert_that(rcv_rx_pps).described_as(
            "Throughput for RECEIVE was below the correct order-of-magnitude"
        ).is_greater_than(2 ** 20)
        assert_that(snd_tx_pps).described_as(
            "Throughput for SEND was below the correct order of magnitude"
        ).is_greater_than(2 ** 20)


def _init_hugepages(node: Node) -> None:
    mount = node.tools[Mount]
    mount.mount(disk_name="nodev", point="/mnt/huge", type="hugetlbfs")
    mount.mount(
        disk_name="nodev",
        point="/mnt/huge-1G",
        type="hugetlbfs",
        options="pagesize=1G",
    )
    _enable_hugepages(node)


def _enable_hugepages(node: Node) -> None:
    echo = node.tools[Echo]
    echo.write_to_file(
        "1024",
        node.get_pure_path(
            "/sys/devices/system/node/node0/hugepages/hugepages-2048kB/nr_hugepages"
        ),
        sudo=True,
    )
    echo.write_to_file(
        "1",
        node.get_pure_path(
            "/sys/devices/system/node/node0/hugepages/hugepages-1048576kB/nr_hugepages"
        ),
        sudo=True,
    )


class DpdkTestResources:
    def __init__(
        self, _node: Node, _node_nic_info: Nics, _testpmd: DpdkTestpmd
    ) -> None:
        self.node_nic_info = _node_nic_info
        self.testpmd = _testpmd
        self.node = _node
        self.nic_controller = _node.features[NetworkInterface]
        self.dmesg = _node.tools[Dmesg]
        self._last_dmesg = ""
        test_nic = self.node_nic_info.get_test_nic()
        # generate hotplug pattern for this specific nic
        self.vf_hotplug_regex = re.compile(
            f"{test_nic.upper}: Data path switched to VF: {test_nic.lower}"
        )
        self.vf_slot_removal_regex = re.compile(f"VF unregistering: {test_nic.lower}")

    def wait_for_dmesg_output(self, wait_for: str, timeout: int) -> bool:
        search_pattern = None
        if wait_for == "AN_DISABLE":
            search_pattern = self.vf_slot_removal_regex
        elif wait_for == "AN_REENABLE":
            search_pattern = self.vf_hotplug_regex
        else:
            raise LisaException(
                "Unknown search pattern specified in "
                "DpdkTestResources:wait_for_dmesg_output"
            )

        self.node.log.info(search_pattern.pattern)
        timer = perf_timer.Timer()
        while timer.elapsed(stop=False) < timeout:
            output = self.dmesg.get_output(force_run=True)
            if search_pattern.search(output.replace(self._last_dmesg, "")):
                self._last_dmesg = output  # save old output to filter next time
                self.node.log.info(
                    f"Found VF hotplug info after {timer.elapsed()} seconds"
                )
                return True
            else:
                time.sleep(1)
        return False


def generate_send_receive_run_info(
    pmd: str,
    sender: DpdkTestResources,
    receiver: DpdkTestResources,
    txq: int = 1,
    rxq: int = 1,
) -> Dict[DpdkTestResources, str]:

    snd_nic, rcv_nic = [x.node_nic_info.get_test_nic() for x in [sender, receiver]]

    snd_cmd = sender.testpmd.generate_testpmd_command(
        snd_nic,
        0,
        "txonly",
        pmd,
        extra_args=f"--tx-ip={snd_nic.ip_addr},{rcv_nic.ip_addr}",
        txq=txq,
        rxq=rxq,
    )
    rcv_cmd = receiver.testpmd.generate_testpmd_command(
        rcv_nic,
        0,
        "rxonly",
        pmd,
        txq=txq,
        rxq=rxq,
    )

    kit_cmd_pairs = {
        sender: snd_cmd,
        receiver: rcv_cmd,
    }

    return kit_cmd_pairs


def bind_nic_to_dpdk_pmd(nics: Nics, nic: NicInfo, pmd: str) -> None:
    if pmd == "netvsc":
        current_driver = nic.bound_driver
        if current_driver == "uio_hv_generic":
            return
        nics.unbind(nic, current_driver)
        # uio_hv_generic needs some special steps to enable
        enable_uio_hv_generic_for_nic(nics._node, nic)
        # bind_dev_to_new_driver
        nics.bind(nic, "uio_hv_generic")
        nic.bound_driver = "uio_hv_generic"
    elif pmd == "failsafe":
        current_driver = nic.bound_driver
        if current_driver == "hv_netvsc":
            return
        nics.unbind(nic, current_driver)
        nics.bind(nic, "hv_netvsc")
        nic.bound_driver = "hv_netvsc"
    else:
        fail(f"Unrecognized pmd {pmd} passed to test init procedure.")


def enable_uio_hv_generic_for_nic(node: Node, nic: NicInfo) -> None:

    # hv_uio_generic driver uuid, a constant value used by vmbus.
    # https://doc.dpdk.org/guides/nics/netvsc.html#installation
    hv_uio_generic_uuid = "f8615163-df3e-46c5-913f-f2d2f965ed0e"

    # using netvsc pmd directly for dpdk on hv counterintuitively requires
    # you to enable to uio_hv_generic driver, steps are found:
    # https://doc.dpdk.org/guides/nics/netvsc.html#installation

    echo = node.tools[Echo]
    node.execute(
        "modprobe uio_hv_generic",
        sudo=True,
        expected_exit_code=0,
        expected_exit_code_failure_message="Could not load uio_hv_generic driver.",
    )
    # vmbus magic to enable uio_hv_generic
    echo.write_to_file(
        hv_uio_generic_uuid,
        node.get_pure_path("/sys/bus/vmbus/drivers/uio_hv_generic/new_id"),
        sudo=True,
    )


def initialize_node_resources(
    node: Node, log: Logger, variables: Dict[str, Any], pmd: str
) -> DpdkTestResources:
    dpdk_source = variables.get("dpdk_source", DPDK_STABLE_GIT)
    dpdk_branch = variables.get("dpdk_branch", "")
    log.info(
        "Dpdk initialize_node_resources running"
        f"found dpdk_source '{dpdk_source}' and dpdk_branch '{dpdk_branch}'"
    )

    network_interface_feature = node.features[NetworkInterface]
    sriov_is_enabled = network_interface_feature.is_enabled_sriov()
    log.info(f"Node[{node.name}] Verify SRIOV is enabled: {sriov_is_enabled}")
    assert_that(sriov_is_enabled).described_as(
        f"SRIOV was not enabled for this test node ({node.name})"
    ).is_true()

    # dump some info about the pci devices before we start
    lspci = node.tools[Lspci]
    log.info(f"Node[{node.name}] LSPCI Info:\n{lspci.run().stdout}\n")
    # init and enable hugepages (required by dpdk)
    _init_hugepages(node)

    # initialize testpmd tool (installs dpdk)
    testpmd = DpdkTestpmd(node)
    testpmd.set_dpdk_source(dpdk_source)
    testpmd.set_dpdk_branch(dpdk_branch)
    testpmd.install()

    # initialize node nic info class (gathers info about nic devices)
    node_nic_info = Nics(node)
    node_nic_info.initialize()
    assert_that(len(node_nic_info)).described_as(
        "Test needs at least 1 NIC on the test node."
    ).is_greater_than_or_equal_to(1)

    # bind test nic to desired pmd
    nic_to_bind = node_nic_info.get_test_nic()
    bind_nic_to_dpdk_pmd(node_nic_info, nic_to_bind, pmd)
    return DpdkTestResources(node, node_nic_info, testpmd)


def _run_testpmd_concurrent(
    node_cmd_pairs: Dict[DpdkTestResources, str],
    seconds: int,
    log: Logger,
    rescind_sriov: bool = False,
) -> Dict[DpdkTestResources, str]:
    output: Dict[DpdkTestResources, str] = dict()
    cmd_pairs_as_tuples = deque(node_cmd_pairs.items())

    def thread_callback(result: Tuple[DpdkTestResources, str]) -> None:
        output[result[0]] = result[1]

    def _run_node_init() -> Tuple[DpdkTestResources, str]:
        # TaskManager doesn't let you pass parameters to your threads
        testkit, cmd = cmd_pairs_as_tuples.pop()
        return (testkit, testkit.testpmd.run_for_n_seconds(cmd, seconds))

    task_manager = TaskManager[Tuple[DpdkTestResources, str]](
        len(cmd_pairs_as_tuples), thread_callback
    )

    for i in range(len(node_cmd_pairs)):
        task_manager.submit_task(
            Task[Tuple[DpdkTestResources, str]](i, _run_node_init, log)
        )
    if rescind_sriov:
        time.sleep(10)  # run testpmd for a bit before disabling sriov
        test_kits = node_cmd_pairs.keys()

        # disable sroiv
        for node_resources in test_kits:
            node_resources.nic_controller.switch_sriov(enable=False)

        # wait for disable to hit the vm
        for node_resources in test_kits:
            if not node_resources.wait_for_dmesg_output("AN_DISABLE", seconds // 3):
                fail(
                    "Accelerated Network disable not found in dmesg"
                    f" before timeout for node {node_resources.node.name}"
                )

        time.sleep(10)  # let testpmd run with sriov disabled

        # re-enable sriov
        for node_resources in test_kits:
            node_resources.nic_controller.switch_sriov(enable=True)

        # wait for re-enable to hit vms
        for node_resources in test_kits:
            if not node_resources.wait_for_dmesg_output("AN_REENABLE", seconds // 2):
                fail(
                    "Accelerated Network re-enable not found "
                    f" in dmesg before timeout for node  {node_resources.node.name}"
                )

        time.sleep(15)  # let testpmd run with sriov re-enabled

        # kill the commands to collect the output early and terminate before timeout
        for node_resources in test_kits:
            node_resources.testpmd.kill_previous_testpmd_command()

    task_manager.wait_for_all_workers()

    return output


def _init_nodes_concurrent(
    environment: Environment, log: Logger, variables: Dict[str, Any], pmd: str
) -> List[DpdkTestResources]:
    # Use threading module to parallelize the IO-bound node init.
    test_kits: List[DpdkTestResources] = []
    nodes = deque(environment.nodes.list())

    def thread_callback(output: DpdkTestResources) -> None:
        test_kits.append(output)

    def run_node_init() -> DpdkTestResources:
        # pop a node from the deque and initialize it.
        node = nodes.pop()
        return initialize_node_resources(node, log, variables, pmd)

    task_manager = TaskManager[DpdkTestResources](
        len(environment.nodes), thread_callback
    )

    for i in range(len(environment.nodes)):
        task_manager.submit_task(Task[DpdkTestResources](i, run_node_init, log))

    task_manager.wait_for_all_workers()

    return test_kits
