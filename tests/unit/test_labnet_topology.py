"""Unit tests for the Exergy-lab.net topology transform."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
import yaml

from transforms.labnet_topology import LabnetTopology


@pytest.mark.parametrize("role", ["spine", "leaf", "border_leaf"])
def test_fabric_roles_export_as_labnet_routers(role: str) -> None:
    """Export every fabric role using the router role accepted by Exergy-lab.net YANG."""
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
    ospf: bool = False,
) -> dict:
    """Build one device edge node for the transform fixture."""
    services: list[dict] = [
        {
            "node": {
                "__typename": "ServiceBGP",
                "session_type": {"value": session_type},
                "local_as": {"node": {"asn": {"value": local}}},
                "remote_as": {"node": {"asn": {"value": remote}}},
            }
        }
        for session_type, local, remote in [
            ("INTERNAL", 4200000111, 4200000111),
            (
                "EXTERNAL",
                4200000222 if role == "spine" else 4200000333,
                4200000333 if role == "spine" else 4200000222,
            ),
        ]
    ]
    if ospf and primary:
        services.append(
            {
                "node": {
                    "__typename": "ServiceOSPF",
                    "router_id": {"node": {"address": {"value": primary}}},
                    "area": {"node": {"area": {"value": "0.0.0.0"}}},
                }
            }
        )
    node: dict = {
        "name": {"value": name},
        "role": {"value": role},
        "platform": {"node": {"nornir_platform": {"value": "eos"}}},
        "interfaces": {"edges": interfaces or []},
        "primary_address": {"node": None},
        "device_services": {"edges": services},
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
    ipv4: str | None = None,
) -> dict:
    """Build a physical interface edge with a cable to a remote fabric port."""
    node: dict = {
        "name": {"value": name},
        "role": {"value": role},
        "interface_services": {"edges": []},
        "ip_addresses": {"edges": []},
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
    if ipv4:
        node["ip_addresses"] = {"edges": [{"node": {"address": {"value": ipv4}}}]}
    return {"node": node}


def _dc_fixture(strategy: str) -> dict:
    """Minimal two-leaf fabric GraphQL payload for the given strategy."""
    ospf = strategy.startswith("ospf")
    return {
        "TopologyDataCenter": {
            "edges": [
                {
                    "node": {
                        "name": {"value": "DC-LAB"},
                        "strategy": {"value": strategy},
                        "devices": {
                            "edges": [
                                {
                                    "node": _device(
                                        "spine1",
                                        "spine",
                                        primary="10.255.0.1/32",
                                        ospf=ospf,
                                        interfaces=[
                                            _cable_interface(
                                                "Ethernet1",
                                                "leaf1",
                                                "Ethernet49",
                                                ipv4="10.0.0.1/31" if ospf else None,
                                            )
                                        ],
                                    )
                                },
                                {
                                    "node": _device(
                                        "leaf1",
                                        "leaf",
                                        primary="10.255.0.11/32",
                                        ospf=ospf,
                                        interfaces=[
                                            _cable_interface(
                                                "Ethernet49",
                                                "spine1",
                                                "Ethernet1",
                                                ipv4="10.0.0.0/31" if ospf else None,
                                            )
                                        ],
                                    )
                                },
                                {
                                    "node": _device(
                                        "leaf2",
                                        "leaf",
                                        primary="10.255.0.12/32",
                                        ospf=ospf,
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


def test_labnet_topology_emits_spines_leafs_and_links() -> None:
    """The transform names fabric devices and deduplicates bidirectional cables."""
    transform = LabnetTopology(client=MagicMock(), infrahub_node=MagicMock())
    topology = yaml.safe_load(asyncio.run(transform.transform(_dc_fixture("ebgp-ibgp"))).split("---")[1])["topology"]

    assert topology["name"] == "DC-LAB"
    assert topology["module"] == ["vlan", "vxlan", "bgp", "evpn"]
    assert set(topology["nodes"]) >= {"spine1", "leaf1", "leaf2"}
    assert "fw1" not in topology["nodes"]
    assert topology["nodes"]["spine1"]["bgp"]["as"] == 4200000222
    assert topology["nodes"]["leaf1"]["bgp"]["local_as"] == 4200000333
    endpoints = {(link_if["node"], link_if["ifname"]) for link in topology["links"] for link_if in link["interfaces"]}
    assert ("spine1", "Ethernet1") in endpoints
    assert ("leaf1", "Ethernet49") in endpoints
    # No invented demo VLAN/hosts when Infrahub has no segments.
    assert "h1" not in topology["nodes"]
    assert "vlans" not in topology or "demo" not in topology.get("vlans", {})


def test_ospf_ibgp_uses_designed_ospf_and_interface_addresses() -> None:
    """ospf-ibgp twins take OSPF and link IPs from Infrahub, not invented /30s."""
    transform = LabnetTopology(client=MagicMock(), infrahub_node=MagicMock())
    topology = yaml.safe_load(asyncio.run(transform.transform(_dc_fixture("ospf-ibgp"))).split("---")[1])["topology"]

    assert topology["module"] == ["ospf", "bgp"]
    assert topology["nodes"]["spine1"]["ospf"] == {"router_id": "10.255.0.1", "area": "0.0.0.0"}
    assert topology["nodes"]["spine1"]["bgp"]["as"] == 4200000111
    assert topology["nodes"]["leaf1"]["bgp"]["as"] == 4200000111
    assert "local_as" not in topology["nodes"]["leaf1"]["bgp"]
    fabric = topology["links"][0]
    assert fabric["type"] == "p2p"
    assert {endpoint["ipv4"] for endpoint in fabric["interfaces"]} == {"10.0.0.1/31", "10.0.0.0/31"}
    assert "h1" not in topology["nodes"]


def test_ospf_ibgp_preserves_unnumbered_fabric_links() -> None:
    """Do not invent underlay addressing when Infrahub left the link unnumbered."""
    data = _dc_fixture("ospf-ibgp")
    for device_edge in data["TopologyDataCenter"]["edges"][0]["node"]["devices"]["edges"]:
        for interface_edge in device_edge["node"]["interfaces"]["edges"]:
            interface_edge["node"]["ip_addresses"] = {"edges": []}
    transform = LabnetTopology(client=MagicMock(), infrahub_node=MagicMock())
    topology = yaml.safe_load(asyncio.run(transform.transform(data)).split("---")[1])["topology"]
    link = topology["links"][0]
    assert link["unnumbered"] is True
    assert "prefix" not in link
    assert all("ipv4" not in endpoint and "ipv6" not in endpoint for endpoint in link["interfaces"])


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
            "ip_addresses": {"edges": []},
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


@pytest.mark.parametrize("vlan_name", ["opsmill", "dc-arista - OpsMill - production - Hypervisor-Management"])
def test_labnet_attaches_hosts_only_on_designed_segments(vlan_name: str) -> None:
    """Twin probe hosts attach only to Infrahub segment ports and prefixes."""
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
                                            _segment_interface("Ethernet10", vlan_name=vlan_name),
                                        ],
                                    )
                                },
                                {
                                    "node": _device(
                                        "leaf2",
                                        "leaf",
                                        primary="10.255.0.12/32",
                                        interfaces=[_segment_interface("Ethernet10", vlan_name=vlan_name)],
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
    assert topology["vlans"][vlan_name.replace(" ", "_")]["id"] == 500
    assert "demo" not in topology["vlans"]
    host_links = [link for link in topology["links"] if link.get("vlan.access") == vlan_name.replace(" ", "_")]
    assert len(host_links) == 2
    assert host_links[0]["interfaces"][0]["ipv4"] == "10.10.50.10/24"


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("eos", "eos"), ("nxos", "nxos"), ("iosxr", "ios-xr"), ("junos", "junos")],
)
def test_source_platform_is_preserved(platform: str, expected: str) -> None:
    """Select the native Exergy-lab.net device flavor from the device's platform."""
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
    with pytest.raises(ValueError, match="Unsupported Exergy-lab.net platform.*router1"):
        asyncio.run(transform.transform(data))


@pytest.mark.parametrize("strategy", ["ospf-ibgp", "ebgp-ibgp"])
def test_bgp_uses_designed_service_asns(strategy: str) -> None:
    """Changing source AS allocations changes exported ASNs without inventing defaults."""
    device = _device(
        "spine1",
        "spine",
        primary="10.255.0.1/32",
        ospf=strategy.startswith("ospf"),
    )
    for edge in device["device_services"]["edges"]:
        service = edge["node"]
        if service["__typename"] != "ServiceBGP":
            continue
        service["local_as"]["node"]["asn"]["value"] = 4212345678
        service["remote_as"]["node"]["asn"]["value"] = (
            4212345678 if service["session_type"]["value"] == "INTERNAL" else 4212345679
        )
    data = {
        "TopologyDataCenter": {
            "edges": [
                {
                    "node": {
                        "strategy": {"value": strategy},
                        "devices": {"edges": [{"node": device}]},
                    }
                }
            ]
        }
    }
    transform = LabnetTopology(client=MagicMock(), infrahub_node=MagicMock())
    topology = yaml.safe_load(asyncio.run(transform.transform(data)).split("---")[1])["topology"]
    assert topology["nodes"]["spine1"]["bgp"]["as"] == 4212345678
    if strategy == "ospf-ibgp":
        assert topology["module"] == ["ospf", "bgp"]
        assert topology["nodes"]["spine1"]["ospf"]["router_id"] == "10.255.0.1"
        assert "local_as" not in topology["nodes"]["spine1"]["bgp"]


def test_missing_designed_asn_does_not_get_a_default() -> None:
    """Fail explicitly instead of inventing a private ASN when services are missing."""
    device = _device("spine1", "spine", primary="10.255.0.1/32", ospf=True)
    device["device_services"] = {
        "edges": [edge for edge in device["device_services"]["edges"] if edge["node"]["__typename"] == "ServiceOSPF"]
    }
    data = {
        "TopologyDataCenter": {
            "edges": [
                {
                    "node": {
                        "strategy": {"value": "ospf-ibgp"},
                        "devices": {"edges": [{"node": device}]},
                    }
                }
            ]
        }
    }
    transform = LabnetTopology(client=MagicMock(), infrahub_node=MagicMock())
    with pytest.raises(ValueError, match="Expected one designed INTERNAL local ASN for spine1"):
        asyncio.run(transform.transform(data))


@pytest.mark.parametrize("role", ["leaf_spine", "unnumbered"])
def test_fabric_links_preserve_ipv6_addresses(role: str) -> None:
    """Preserve IPv6-only links and explicit unnumbered roles without inventing IPv4."""
    data = _dc_fixture("ospf-ibgp")
    devices = data["TopologyDataCenter"]["edges"][0]["node"]["devices"]["edges"]
    for index, edge in enumerate(devices):
        for interface_edge in edge["node"]["interfaces"]["edges"]:
            interface = interface_edge["node"]
            interface["role"] = {"value": role}
            interface["ip_addresses"] = {
                "edges": [
                    {
                        "node": {
                            "address": {"value": f"2001:db8::{index + 1}/64"},
                        }
                    }
                ]
            }
    transform = LabnetTopology(client=MagicMock(), infrahub_node=MagicMock())
    topology = yaml.safe_load(asyncio.run(transform.transform(data)).split("---")[1])["topology"]
    link = topology["links"][0]
    assert all("ipv6" in endpoint and "ipv4" not in endpoint for endpoint in link["interfaces"])
    assert link.get("unnumbered", False) is (role == "unnumbered")
