"""Unit tests for the lab.net topology transform."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import yaml

from transforms.labnet_topology import LabnetTopology


def _device(
    name: str,
    role: str,
    *,
    primary: str | None = None,
    interfaces: list[dict] | None = None,
) -> dict:
    """Build one device edge node for the transform fixture.

    Args:
        name: Device name.
        role: Device role.
        primary: Optional primary address string.
        interfaces: Optional interface edge list.

    Returns:
        GraphQL-shaped device node.
    """
    node: dict = {
        "name": {"value": name},
        "role": {"value": role},
        "interfaces": {"edges": interfaces or []},
        "primary_address": {"node": None},
    }
    if primary:
        node["primary_address"] = {"node": {"address": {"value": primary}}}
    return node


def _cable_interface(
    name: str,
    remote_device: str,
    remote_if: str,
    *,
    role: str = "leaf_spine",
) -> dict:
    """Build a physical interface edge with a cable to a remote fabric port."""
    return {
        "node": {
            "name": {"value": name},
            "role": {"value": role},
            "interface_services": {"edges": []},
            "connector": {
                "node": {
                    "connected_endpoints": {
                        "edges": [
                            {
                                "node": {
                                    "name": {"value": name},
                                    "role": {"value": role},
                                    "device": {"node": {"name": {"value": "ignored"}}},
                                }
                            },
                            {
                                "node": {
                                    "name": {"value": remote_if},
                                    "role": {"value": role},
                                    "device": {"node": {"name": {"value": remote_device}}},
                                }
                            },
                        ]
                    }
                }
            },
        }
    }


def test_labnet_topology_emits_spines_leafs_and_links() -> None:
    """The transform names fabric devices and deduplicates bidirectional cables."""
    data = {
        "TopologyDataCenter": {
            "edges": [
                {
                    "node": {
                        "name": {"value": "DC-LAB"},
                        "strategy": {"value": "ebgp-ibgp"},
                        "devices": {
                            "edges": [
                                {
                                    "node": _device(
                                        "spine1",
                                        "spine",
                                        primary="10.255.0.1/32",
                                        interfaces=[_cable_interface("Ethernet1", "leaf1", "Ethernet49")],
                                    )
                                },
                                {
                                    "node": _device(
                                        "leaf1",
                                        "leaf",
                                        primary="10.255.0.11/32",
                                        interfaces=[_cable_interface("Ethernet49", "spine1", "Ethernet1")],
                                    )
                                },
                                {
                                    "node": _device(
                                        "leaf2",
                                        "leaf",
                                        primary="10.255.0.12/32",
                                        interfaces=[],
                                    )
                                },
                                {"node": _device("fw1", "firewall", interfaces=[])},
                            ]
                        },
                    }
                }
            ]
        }
    }

    transform = LabnetTopology(client=MagicMock(), infrahub_node=MagicMock())
    xp_text = asyncio.run(transform.transform(data))
    document = yaml.safe_load(xp_text.split("---")[1])
    topology = document["topology"]

    assert topology["name"] == "DC-LAB"
    assert topology["module"] == ["vlan", "vxlan", "bgp", "evpn"]
    assert set(topology["nodes"]) >= {"spine1", "leaf1", "leaf2"}
    assert "fw1" not in topology["nodes"]
    assert topology["nodes"]["spine1"]["bgp"]["as"] == 65100
    assert topology["nodes"]["leaf1"]["bgp"]["local_as"] == 65011
    assert len(topology["links"]) == 1
    endpoints = {(link_if["node"], link_if["ifname"]) for link in topology["links"] for link_if in link["interfaces"]}
    assert ("spine1", "Ethernet1") in endpoints
    assert ("leaf1", "Ethernet49") in endpoints
