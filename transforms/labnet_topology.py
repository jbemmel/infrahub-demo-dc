"""Transform a TopologyDataCenter into an Exergy-lab.net .xp topology document.

All fabric addressing, ASNs, and OSPF parameters come from the Infrahub design
(GraphQL). This transform does not invent underlay subnets or routing policy.
"""

from __future__ import annotations

from typing import Any

import yaml
from infrahub_sdk.transforms import InfrahubTransform

FABRIC_ROLES = frozenset({"spine", "leaf", "border_leaf"})
PLATFORM_DEVICES = {
    "eos": "eos",
    "iosxe": "iosxe",
    "iosxr": "ios-xr",
    "nxos": "nxos",
    "junos": "junos",
    "sonic": "sonic-vs",
    "frr": "frr",
}
SKIP_INTERFACE_ROLES = frozenset({"console", "management"})
VNI_OFFSET = 10000
OSPF_IBGP_STRATEGIES = frozenset({"ospf-ibgp", "ospf"})
EVPN_EBGP_STRATEGIES = frozenset({"ebgp-ibgp"})
DEMO_STRATEGIES = OSPF_IBGP_STRATEGIES | EVPN_EBGP_STRATEGIES


def _attr(node: dict[str, Any] | None, name: str) -> Any:
    """Return ``node[name].value`` when present."""
    if not isinstance(node, dict):
        return None
    field = node.get(name)
    if isinstance(field, dict) and "value" in field:
        return field.get("value")
    return None


def _designed_asn(device: dict[str, Any], session_type: str) -> int:
    """Read the local ASN from designed BGP services, rejecting missing or conflicting data."""
    name = _attr(device, "name")
    values: set[int] = set()
    for edge in (device.get("device_services") or {}).get("edges") or []:
        service = edge.get("node") or {}
        if service.get("__typename") != "ServiceBGP" or _attr(service, "session_type") != session_type:
            continue
        value = _attr((service.get("local_as") or {}).get("node"), "asn")
        remote = _attr((service.get("remote_as") or {}).get("node"), "asn")
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 4294967295:
            raise ValueError(f"Invalid designed {session_type} local ASN for {name}: {value!r}")
        if isinstance(remote, bool) or not isinstance(remote, int) or not 1 <= remote <= 4294967295:
            raise ValueError(f"Invalid designed {session_type} remote ASN for {name}: {remote!r}")
        if (value == remote) != (session_type == "INTERNAL"):
            raise ValueError(f"Designed {session_type} AS mismatch for {name}: {value}/{remote}")
        values.add(value)
    if len(values) != 1:
        raise ValueError(f"Expected one designed {session_type} local ASN for {name}, found {sorted(values)}")
    return next(iter(values))


def _designed_ospf(device: dict[str, Any]) -> dict[str, Any]:
    """Return Exergy-lab.net OSPF node config from designed ServiceOSPF objects."""
    name = _attr(device, "name")
    router_ids: set[str] = set()
    areas: set[str] = set()
    for edge in (device.get("device_services") or {}).get("edges") or []:
        service = edge.get("node") or {}
        if service.get("__typename") != "ServiceOSPF":
            continue
        router_id = _attr((service.get("router_id") or {}).get("node"), "address")
        if isinstance(router_id, str) and router_id:
            router_ids.add(router_id.split("/")[0])
        area = _attr((service.get("area") or {}).get("node"), "area")
        if isinstance(area, int) and not isinstance(area, bool):
            area = str(area)
        if isinstance(area, str) and area:
            areas.add(area)
    if len(router_ids) != 1:
        raise ValueError(f"Expected one designed OSPF router-id for {name}, found {sorted(router_ids)}")
    if len(areas) != 1:
        raise ValueError(f"Expected one designed OSPF area for {name}, found {sorted(areas)}")
    return {"router_id": next(iter(router_ids)), "area": next(iter(areas))}


def _interface_ipv4(interface: dict[str, Any]) -> str | None:
    """Return the first IPv4 CIDR on an interface, if designed."""
    for edge in (interface.get("ip_addresses") or {}).get("edges") or []:
        address = _attr(edge.get("node"), "address")
        if isinstance(address, str) and address and "." in address.split("/")[0]:
            return address
    return None


def _interface_index(devices: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Map ``(device_name, ifname)`` to the interface GraphQL node."""
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for device in devices:
        device_name = _attr(device, "name")
        if not device_name:
            continue
        for interface_edge in device.get("interfaces", {}).get("edges") or []:
            interface = interface_edge.get("node") or {}
            ifname = _attr(interface, "name")
            if ifname:
                index[(device_name, ifname)] = interface
    return index


def _host_ip(prefix: str, host_index: int) -> str | None:
    """Return ``prefix`` with the host octet set to ``host_index`` (IPv4 only)."""
    try:
        network, length = prefix.split("/", 1)
        octets = [int(part) for part in network.split(".")]
        if len(octets) != 4:
            return None
        octets[3] = host_index
        return f"{'.'.join(str(part) for part in octets)}/{length}"
    except (ValueError, IndexError):
        return None


def _segment_on_interface(interface: dict[str, Any]) -> dict[str, Any] | None:
    """Return the first ServiceNetworkSegment on an interface, if any."""
    for service_edge in interface.get("interface_services", {}).get("edges") or []:
        service = service_edge.get("node") or {}
        if service.get("__typename") == "ServiceNetworkSegment":
            return service
    return None


class LabnetTopology(InfrahubTransform):
    """Render an Exergy-lab.net browser-twin topology from a data center deployment."""

    query = "topology_labnet"

    async def transform(self, data: dict[str, Any]) -> str:
        """Build an ``.xp`` document for Exergy-lab.net from TopologyDataCenter GraphQL data.

        Args:
            data: GraphQL result for ``topology_labnet``.

        Returns:
            YAML ``.xp`` text with front-matter topology.
        """
        edges = data.get("TopologyDataCenter", {}).get("edges") or []
        if not edges:
            raise ValueError("TopologyDataCenter not found for Exergy-lab.net transform")

        dc = edges[0]["node"]
        name = _attr(dc, "name") or "topology"
        strategy = _attr(dc, "strategy") or ""

        fabric_devices: list[dict[str, Any]] = []
        for device_edge in dc.get("devices", {}).get("edges") or []:
            device = device_edge.get("node") or {}
            if _attr(device, "role") in FABRIC_ROLES:
                fabric_devices.append(device)

        fabric_names = {_attr(device, "name") or "" for device in fabric_devices}
        interfaces_by_key = _interface_index(fabric_devices)

        nodes: dict[str, Any] = {}
        links: list[dict[str, Any]] = []
        seen_links: set[tuple[tuple[str, str], tuple[str, str]]] = set()
        vlans: dict[str, Any] = {}

        leafs = sorted(
            (device for device in fabric_devices if _attr(device, "role") in {"leaf", "border_leaf"}),
            key=lambda device: _attr(device, "name") or "",
        )

        for device in fabric_devices:
            device_name = _attr(device, "name")
            if not device_name:
                continue
            role = _attr(device, "role")
            # Exergy-lab.net YANG only allows router|host|switch|bridge (not fabric roles).
            platform = _attr((device.get("platform") or {}).get("node"), "nornir_platform")
            if platform not in PLATFORM_DEVICES:
                raise ValueError(f"Unsupported Exergy-lab.net platform {platform!r} for {device_name}")
            node: dict[str, Any] = {"device": PLATFORM_DEVICES[platform], "role": "router"}
            primary_node = (device.get("primary_address") or {}).get("node") or {}
            primary = _attr(primary_node, "address")
            loopback_ip = primary.split("/")[0] if isinstance(primary, str) and primary else None

            if strategy in OSPF_IBGP_STRATEGIES:
                node["ospf"] = _designed_ospf(device)
                if loopback_ip:
                    node["loopback"] = {"ipv4": f"{loopback_ip}/32"}
                node["bgp"] = {"as": _designed_asn(device, "INTERNAL")}
            elif strategy in EVPN_EBGP_STRATEGIES:
                if role == "spine":
                    node["bgp"] = {"as": _designed_asn(device, "EXTERNAL")}
                else:
                    bgp: dict[str, Any] = {
                        "as": _designed_asn(device, "INTERNAL"),
                        "local_as": _designed_asn(device, "EXTERNAL"),
                    }
                    if loopback_ip:
                        node["loopback"] = {"ipv4": f"{loopback_ip}/32"}
                        node["vxlan"] = {"vtep": loopback_ip}
                        bgp["advertise"] = [{"ipv4": f"{loopback_ip}/32"}]
                    node["bgp"] = bgp
            nodes[device_name] = node

        for device in fabric_devices:
            source_name = _attr(device, "name")
            if not source_name:
                continue
            for interface_edge in device.get("interfaces", {}).get("edges") or []:
                interface = interface_edge.get("node") or {}
                source_if = _attr(interface, "name")
                if not source_if or _attr(interface, "role") in SKIP_INTERFACE_ROLES:
                    continue

                segment = _segment_on_interface(interface)
                if segment is not None:
                    vlan_id = _attr(segment, "vlan_id")
                    if vlan_id is not None:
                        vlan_key = (_attr(segment, "name") or f"vlan{vlan_id}").replace(" ", "_")
                        if vlan_key not in vlans:
                            vlan_entry: dict[str, Any] = {
                                "id": int(vlan_id),
                                "mode": "bridge",
                                "vni": int(vlan_id) + VNI_OFFSET,
                            }
                            prefix_node = (segment.get("prefix") or {}).get("node") or {}
                            prefix = _attr(prefix_node, "prefix")
                            if prefix:
                                vlan_entry["prefix"] = {"ipv4": prefix}
                            vlans[vlan_key] = vlan_entry

                cable = (interface.get("connector") or {}).get("node")
                if not cable:
                    continue
                for endpoint_edge in cable.get("connected_endpoints", {}).get("edges") or []:
                    endpoint = endpoint_edge.get("node") or {}
                    remote_name = ((endpoint.get("device") or {}).get("node") or {}).get("name", {})
                    remote_name = remote_name.get("value") if isinstance(remote_name, dict) else None
                    remote_if = _attr(endpoint, "name")
                    if not remote_name or not remote_if:
                        continue
                    if remote_name == source_name and remote_if == source_if:
                        continue
                    if remote_name not in fabric_names:
                        continue
                    if _attr(endpoint, "role") in SKIP_INTERFACE_ROLES:
                        continue
                    left = (source_name, source_if)
                    right = (remote_name, remote_if)
                    key: tuple[tuple[str, str], tuple[str, str]] = (left, right) if left <= right else (right, left)
                    if key in seen_links:
                        continue
                    seen_links.add(key)

                    left_endpoint: dict[str, Any] = {"node": source_name, "ifname": source_if}
                    right_endpoint: dict[str, Any] = {"node": remote_name, "ifname": remote_if}
                    remote_interface = interfaces_by_key.get((remote_name, remote_if), {})
                    for source, target in [(interface, left_endpoint), (remote_interface, right_endpoint)]:
                        for address_edge in (source.get("ip_addresses") or {}).get("edges") or []:
                            address = _attr(address_edge.get("node"), "address")
                            if isinstance(address, str) and address:
                                family = "ipv6" if ":" in address else "ipv4"
                                target.setdefault(family, address)
                    link: dict[str, Any] = {"interfaces": [left_endpoint, right_endpoint]}
                    if (
                        _attr(interface, "role") == "unnumbered"
                        or _attr(remote_interface, "role") == "unnumbered"
                        or any(
                            not (endpoint.get("ipv4") or endpoint.get("ipv6"))
                            for endpoint in [left_endpoint, right_endpoint]
                        )
                    ):
                        link["unnumbered"] = True
                    if strategy in OSPF_IBGP_STRATEGIES:
                        link["type"] = "p2p"
                    links.append(link)

        host_names: list[str] = []
        if strategy in DEMO_STRATEGIES and len(leafs) >= 1:
            used_ports = {
                (endpoint["node"], endpoint["ifname"]) for link in links for endpoint in link.get("interfaces") or []
            }
            host_names = _attach_segment_hosts(
                leafs=leafs,
                nodes=nodes,
                links=links,
                vlans=vlans,
                used_ports=used_ports,
            )

        topology: dict[str, Any] = {
            "_concept": "topology",
            "layout": "tree",
            "name": name,
            "module": [],
            "nodes": nodes,
            "links": links,
        }
        spines = [device for device in fabric_devices if _attr(device, "role") == "spine"]
        if strategy in OSPF_IBGP_STRATEGIES:
            topology["module"] = ["ospf", "bgp"]
            if vlans:
                topology["module"] = ["ospf", "bgp", "vlan", "vxlan", "evpn"]
                topology["vxlan"] = {"flooding": "evpn"}
                topology["vlans"] = vlans
            if len(host_names) >= 2:
                topology["validate"] = _demo_validate_steps(
                    host_names=host_names,
                    leafs=leafs,
                    spines=spines,
                    ospf=True,
                )
        elif strategy in EVPN_EBGP_STRATEGIES:
            topology["module"] = ["vlan", "vxlan", "bgp", "evpn"]
            topology["vxlan"] = {"flooding": "evpn"}
            if vlans:
                topology["vlans"] = vlans
            if len(host_names) >= 2:
                topology["validate"] = _demo_validate_steps(
                    host_names=host_names,
                    leafs=leafs,
                    spines=spines,
                )
        elif strategy:
            topology["module"] = ["bgp"]

        document = {
            "id": name,
            "title": f"Browser twin: {name}",
            "summary": f"Exergy-lab.net twin of Infrahub topology {name}",
            "_id": name,
            "topology": topology,
        }
        return f"---\n{yaml.safe_dump(document, sort_keys=False)}---\n"


def _attach_segment_hosts(
    *,
    leafs: list[dict[str, Any]],
    nodes: dict[str, Any],
    links: list[dict[str, Any]],
    vlans: dict[str, Any],
    used_ports: set[tuple[str, str]],
) -> list[str]:
    """Attach twin probe hosts only on Infrahub-designed segment ports.

    Hosts are not Infrahub objects; they are browser-twin probes attached to
    designed VLANs/prefixes so ping and route tables are exercisable. Underlay
    addressing and ASNs remain untouched.
    """
    host_names: list[str] = []
    targets = leafs[:2] if len(leafs) >= 2 else ([leafs[0], leafs[0]] if leafs else [])
    shared_vlan: str | None = None
    shared_prefix: str | None = None

    for leaf in targets:
        leaf_name = _attr(leaf, "name")
        if not leaf_name:
            continue
        attachment = _segment_attachment(leaf, used_ports)
        if attachment is None:
            continue
        ifname, vlan_key, vlan_id, prefix = attachment
        if vlan_key not in vlans:
            vlans[vlan_key] = {
                "id": vlan_id,
                "mode": "bridge",
                "vni": vlan_id + VNI_OFFSET,
                "prefix": {"ipv4": prefix},
            }
        if shared_vlan is None:
            shared_vlan = vlan_key
            shared_prefix = prefix
        else:
            vlan_key = shared_vlan
            prefix = shared_prefix or prefix

        host_name = f"h{len(host_names) + 1}"
        host_names.append(host_name)
        nodes[host_name] = {"device": "linux", "role": "host"}
        host_endpoint: dict[str, Any] = {"node": host_name, "ifname": "eth1"}
        host_ip = _host_ip(prefix, 9 + len(host_names))
        if host_ip:
            host_endpoint["ipv4"] = host_ip
        links.append(
            {
                "vlan.access": vlan_key,
                "interfaces": [
                    host_endpoint,
                    {"node": leaf_name, "ifname": ifname},
                ],
            }
        )
        used_ports.add((leaf_name, ifname))

    if len(host_names) >= 2:
        nodes[host_names[0]]["validation"] = {
            "actions": [
                {
                    "label": f"ping {host_names[1]}",
                    "type": "ping",
                    "target": host_names[1],
                }
            ]
        }
    return host_names


def _segment_attachment(
    leaf: dict[str, Any],
    used_ports: set[tuple[str, str]],
) -> tuple[str, str, int, str] | None:
    """Return ``(ifname, vlan_key, vlan_id, prefix)`` for a designed segment port."""
    leaf_name = _attr(leaf, "name")
    if not leaf_name:
        return None
    for interface_edge in leaf.get("interfaces", {}).get("edges") or []:
        interface = interface_edge.get("node") or {}
        ifname = _attr(interface, "name")
        if not ifname or _attr(interface, "role") in SKIP_INTERFACE_ROLES:
            continue
        if (leaf_name, ifname) in used_ports:
            continue
        segment = _segment_on_interface(interface)
        if segment is None:
            continue
        vlan_id = _attr(segment, "vlan_id")
        prefix_node = (segment.get("prefix") or {}).get("node") or {}
        prefix = _attr(prefix_node, "prefix")
        if vlan_id is None or not prefix:
            continue
        vlan_key = (_attr(segment, "name") or f"vlan{vlan_id}").replace(" ", "_")
        return ifname, vlan_key, int(vlan_id), str(prefix)
    return None


def _demo_validate_steps(
    *,
    host_names: list[str],
    leafs: list[dict[str, Any]],
    spines: list[dict[str, Any]],
    ospf: bool = False,
) -> list[dict[str, Any]]:
    """Build topology.validate steps: underlay/BGP up, then cross-host ICMP."""
    steps: list[dict[str, Any]] = []
    spine_name = _attr(spines[0], "name") if spines else None
    leaf_name = _attr(leafs[0], "name") if leafs else None
    if spine_name and leaf_name and ospf:
        steps.append(
            {
                "label": "OSPF reaches the spine loopback",
                "state": [
                    {
                        "node": leaf_name,
                        "routes": [
                            {
                                "prefix": f"{spine_name}.loopback",
                                "protocol": "ospf",
                            }
                        ],
                    }
                ],
            }
        )
    if spine_name and leaf_name:
        steps.append(
            {
                "label": "leaf-spine BGP is up",
                "state": [
                    {
                        "node": leaf_name,
                        "bgp": [{"peer": spine_name, "state": "established"}],
                    }
                ],
            }
        )
    steps.append(
        {
            "label": f"{host_names[0]} reaches {host_names[1]}",
            "packet": {
                "from": host_names[0],
                "type": "icmp-echo",
                "target": f"{host_names[1]}.ipv4",
                "expect": {
                    "type": "icmp-echo-reply",
                    "from": host_names[1],
                },
            },
        }
    )
    return steps
