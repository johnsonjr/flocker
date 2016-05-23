# Copyright ClusterHQ Inc.  See LICENSE file for details.

"""
Tools for testing :py:module:`flocker.control`.
"""

from zope.interface.verify import verifyObject

from twisted.internet.endpoints import TCP4ServerEndpoint
from twisted.internet.ssl import ClientContextFactory
from twisted.internet.task import Clock
from twisted.test.proto_helpers import MemoryReactor

from ..testtools import TestCase

from ._clusterstate import ClusterStateService
from ._persistence import ConfigurationPersistenceService
from ._protocol import (
    ControlAMPService, ControlAMP,
)
from ._registry import IStatePersister, InMemoryStatePersister
from ._model import (
    DatasetAlreadyOwned, Lease, PersistentState, Node,
    Deployment
)

from ..testtools.amp import (
    LoopbackAMPClient,
)

from hypothesis import given, assume
import hypothesis.strategies as st
from hypothesis.strategies import uuids, text, composite
from hypothesis.extra.datetime import datetimes

__all__ = [
    'build_control_amp_service',
    'InMemoryStatePersister',
    'make_istatepersister_tests',
    'make_loopback_control_client',
]


def make_istatepersister_tests(fixture):
    """
    Create a TestCase for ``IStatePersister``.

    :param fixture: A fixture that returns a tuple of
    :class:`IStatePersister` provider and a 0-argument callable that
        returns a ``PersistentState``.
    """
    class IStatePersisterTests(TestCase):
        """
        Tests for ``IStatePersister`` implementations.
        """

        def test_interface(self):
            """
            The object implements ``IStatePersister``.
            """
            state_persister, get_state = fixture(self)
            verifyObject(IStatePersister, state_persister)

        @given(
            dataset_id=uuids(),
            blockdevice_id=text(),
        )
        def test_records_blockdeviceid(self, dataset_id, blockdevice_id):
            """
            Calling ``record_ownership`` records the blockdevice
            mapping in the state.
            """
            state_persister, get_state = fixture(self)
            d = state_persister.record_ownership(
                dataset_id=dataset_id,
                blockdevice_id=blockdevice_id,
            )
            self.successResultOf(d)
            self.assertEqual(
                get_state().blockdevice_ownership[dataset_id],
                blockdevice_id,
            )

        @given(
            dataset_id=uuids(),
            blockdevice_id=text(),
            other_blockdevice_id=text()
        )
        def test_duplicate_blockdevice_id(
            self, dataset_id, blockdevice_id, other_blockdevice_id
        ):
            """
            Calling ``record_ownership`` raises
            ``DatasetAlreadyOwned`` if the dataset already has a
            associated blockdevice.
            """
            assume(blockdevice_id != other_blockdevice_id)
            state_persister, get_state = fixture(self)
            self.successResultOf(state_persister.record_ownership(
                dataset_id=dataset_id,
                blockdevice_id=blockdevice_id,
            ))
            self.failureResultOf(state_persister.record_ownership(
                dataset_id=dataset_id,
                blockdevice_id=other_blockdevice_id,
            ), DatasetAlreadyOwned)
            self.assertEqual(
                get_state().blockdevice_ownership[dataset_id],
                blockdevice_id,
            )

    return IStatePersisterTests


def build_control_amp_service(test_case, reactor=None):
    """
    Create a new ``ControlAMPService``.

    :param TestCase test_case: The test this service is for.

    :return ControlAMPService: Not started.
    """
    if reactor is None:
        reactor = Clock()
    cluster_state = ClusterStateService(reactor)
    cluster_state.startService()
    test_case.addCleanup(cluster_state.stopService)
    persistence_service = ConfigurationPersistenceService(
        reactor, test_case.make_temporary_directory())
    persistence_service.startService()
    test_case.addCleanup(persistence_service.stopService)
    return ControlAMPService(
        reactor, cluster_state, persistence_service,
        TCP4ServerEndpoint(MemoryReactor(), 1234),
        # Easiest TLS context factory to create:
        ClientContextFactory(),
    )


def make_loopback_control_client(test_case, reactor):
    """
    Create a control service and a client connected to it.

    :return: A tuple of a ``ControlAMPService`` and a
        ``LoopbackAMPClient`` connected to it.
    """
    control_amp_service = build_control_amp_service(test_case, reactor=reactor)
    client = LoopbackAMPClient(
        command_locator=ControlAMP(reactor, control_amp_service).locator,
    )
    return control_amp_service, client


@composite
def persistent_state_strategy(draw):
    return PersistentState()


@composite
def lease_strategy(draw, dataset_id=st.uuids(), node_id=st.uuids()):
    return Lease(
        dataset_id=draw(dataset_id),
        node_id=draw(node_id),
        expiration=draw(datetimes())
    )


@composite
def node_strategy(draw):
    return Node(
        uuid=draw(st.uuids())
    )


@composite
def deployment_strategy(draw, min_number_of_nodes=1):
    nodes = draw(
        st.lists(
            node_strategy(),
            min_size=min_number_of_nodes,
            average_size=max(min_number_of_nodes, 5),
            max_size=max(min_number_of_nodes, 1000)
        )
    )
    dataset_id_node_mapping = {}
    for node in nodes:
        for dataset_id in node.manifestations:
            dataset_id_node_mapping[dataset_id] = node.uuid

    lease_indexes = []
    if len(dataset_id_node_mapping) > 0:
        lease_indexes = draw(st.sets(
            st.integers(
                min_value=0, max_value=(len(dataset_id_node_mapping)-1)
            )
        ))
    leases = [
        draw(
            lease_strategy(
                dataset_id=st.just(dataset_id),
                node_uuid=st.just(node_uuid)
            )
        ) for dataset_id, node_uuid in (
            dataset_id_node_mapping.items()[i] for i in lease_indexes
        )
    ]
    persistent_state = draw(persistent_state_strategy())
    return Deployment(
        nodes={n.uuid: n for n in nodes},
        leases={l.dataset_id: l for l in leases},
        persistent_state=persistent_state
    )
