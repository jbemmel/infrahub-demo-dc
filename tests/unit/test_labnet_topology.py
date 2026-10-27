"""Unit tests for the lab.net topology transform."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
import yaml

from transforms.labnet_topology import LabnetTopology


@pytest.mark.parametrize("role", ["spine", "leaf", "border_leaf"])
def test_fabric_roles_export_as_labnet_routers(role: str) -> None:
    """Export every fabric role using the router role accepted by lab.net YANG."""
    data = {
        "TopologyDataCenter": {
            "edges": [
                {
                    "node": {
                        "name": {"value": "DC-LAB"},
                        "devices": {"edges": [{"node": _device("router1", role)}]},
                    }
                }
            ]
        }
    }
    transform = LabnetTopology(client=MagicMock(), infrahub_node=MagicMock())
    document = yaml.safe_load(asyncio.run(transform.transform(data)).split("---")[1])

    assert document["topology"]["nodes"]["router1"]["role"] == "router"
    assert document["topology"]["nodes"]["router1"]["device"] == "eos"


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
        "platform": {"node": {"nornir_platform": {"value": "eos"}}},
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
    endpoints = {(link_if["node"], link_if["ifname"]) for link in topology["links"] for link_if in link["interfaces"]}
    assert ("spine1", "Ethernet1") in endpoints
    assert ("leaf1", "Ethernet49") in endpoints
    # Synthetic clients always appear so the browser twin has a ping story.
    assert {"h1", "h2"} <= set(topology["nodes"])
    assert topology["vlans"]["demo"]["id"] == 100
    assert topology["nodes"]["h1"]["validation"]["actions"][0]["target"] == "h2"
    assert topology["validate"][-1]["packet"]["from"] == "h1"


def _segment_interface(
    name: str,
    *,
    vlan_id: int = 500,
    vlan_name: str = "opsmill",
    prefix: str = "10.10.50.0/24",
) -> dict:
    """Build a leaf access interface with a ServiceNetworkSegment."""
    return {
        "node": {
            "name": {"value": name},
            "role": {"value": "server"},
            "interface_services": {
                "edges": [
                    {
                        "node": {
                            "__typename": "ServiceNetworkSegment",
                            "name": {"value": vlan_name},
                            "vlan_id": {"value": vlan_id},
                            "prefix": {"node": {"prefix": {"value": prefix}}},
                        }
                    }
                ]
            },
            "connector": {"node": None},
        }
    }


def test_labnet_prefers_segment_ports_for_demo_hosts() -> None:
    """When leafs expose segments, hosts attach there on that VLAN."""
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
                                        interfaces=[
                                            _cable_interface("Ethernet49", "spine1", "Ethernet1"),
                                            _segment_interface("Ethernet10"),
                                        ],
                                    )
                                },
                                {
                                    "node": _device(
                                        "leaf2",
                                        "leaf",
                                        primary="10.255.0.12/32",
                                        interfaces=[_segment_interface("Ethernet10")],
                                    )
                                },
                            ]
                        },
                    }
                }
            ]
        }
    }
    transform = LabnetTopology(client=MagicMock(), infrahub_node=MagicMock())
    topology = yaml.safe_load(asyncio.run(transform.transform(data)).split("---")[1])["topology"]
    assert topology["vlans"]["opsmill"]["id"] == 500
    assert "demo" not in topology["vlans"]
    host_links = [link for link in topology["links"] if link.get("vlan.access") == "opsmill"]
    assert len(host_links) == 2
    assert host_links[0]["interfaces"][0]["ipv4"] == "10.10.50.10/24"


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("eos", "eos"), ("nxos", "nxos"), ("iosxr", "ios-xr"), ("junos", "junos")],
)
def test_source_platform_is_preserved(platform: str, expected: str) -> None:
    """Select the native lab.net device flavor from the device's platform."""
    device = _device("router1", "spine")
    device["platform"]["node"]["nornir_platform"]["value"] = platform
    data = {
        "TopologyDataCenter": {
            "edges": [
                {
                    "node": {
                        "devices": {"edges": [{"node": device}]},
                    }
                }
            ]
        }
    }
    transform = LabnetTopology(client=MagicMock(), infrahub_node=MagicMock())
    document = yaml.safe_load(asyncio.run(transform.transform(data)).split("---")[1])
    assert document["topology"]["nodes"]["router1"]["device"] == expected
    assert "device" not in document["topology"]


@pytest.mark.parametrize("platform", [None, "unknown-os"])
def test_unsupported_platform_is_rejected(platform: str | None) -> None:
    """Missing or unsupported platforms must not silently become FRR devices."""
    device = _device("router1", "spine")
    device["platform"]["node"]["nornir_platform"]["value"] = platform
    data = {
        "TopologyDataCenter": {
            "edges": [
                {
                    "node": {
                        "devices": {"edges": [{"node": device}]},
                    }
                }
            ]
        }
    }
    transform = LabnetTopology(client=MagicMock(), infrahub_node=MagicMock())
    with pytest.raises(ValueError, match="Unsupported lab.net platform.*router1"):
        asyncio.run(transform.transform(data))
