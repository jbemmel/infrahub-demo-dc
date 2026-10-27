"""Transform a TopologyDataCenter into a lab.net .xp topology document."""

from __future__ import annotations

from typing import Any

import yaml
from infrahub_sdk.transforms import InfrahubTransform

FABRIC_ROLES = frozenset({"spine", "leaf", "border_leaf"})
SKIP_INTERFACE_ROLES = frozenset({"console", "management"})
SPINE_AS = 65100
LEAF_OVERLAY_AS = 65000
LEAF_LOCAL_AS_BASE = 65010
VNI_OFFSET = 10000


def _attr(node: dict[str, Any] | None, name: str) -> Any:
    """Return ``node[name].value`` when present."""
    if not isinstance(node, dict):
        return None
    field = node.get(name)
    if isinstance(field, dict) and "value" in field:
        return field.get("value")
    return None


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
    """Render a lab.net browser-twin topology from a data center deployment."""

    query = "topology_labnet"

    async def transform(self, data: dict[str, Any]) -> str:
        """Build an ``.xp`` document for lab.net from TopologyDataCenter GraphQL data.

        Args:
            data: GraphQL result for ``topology_labnet``.

        Returns:
            YAML ``.xp`` text with front-matter topology.
        """
        edges = data.get("TopologyDataCenter", {}).get("edges") or []
        if not edges:
            raise ValueError("TopologyDataCenter not found for lab.net transform")

        dc = edges[0]["node"]
        name = _attr(dc, "name") or "topology"
        strategy = _attr(dc, "strategy") or ""

        fabric_devices: list[dict[str, Any]] = []
        for device_edge in dc.get("devices", {}).get("edges") or []:
            device = device_edge.get("node") or {}
            if _attr(device, "role") in FABRIC_ROLES:
                fabric_devices.append(device)

        fabric_names = {_attr(device, "name") or "" for device in fabric_devices}

        nodes: dict[str, Any] = {}
        links: list[dict[str, Any]] = []
        seen_links: set[tuple[tuple[str, str], tuple[str, str]]] = set()
        vlans: dict[str, Any] = {}

        leafs = sorted(
            (device for device in fabric_devices if _attr(device, "role") in {"leaf", "border_leaf"}),
            key=lambda device: _attr(device, "name") or "",
        )
        leaf_index = {(_attr(device, "name") or ""): index + 1 for index, device in enumerate(leafs)}

        for device in fabric_devices:
            device_name = _attr(device, "name")
            if not device_name:
                continue
            role = _attr(device, "role")
            node: dict[str, Any] = {"device": "frr", "role": role}
            primary_node = (device.get("primary_address") or {}).get("node") or {}
            primary = _attr(primary_node, "address")
            loopback_ip = primary.split("/")[0] if isinstance(primary, str) and primary else None

            if strategy == "ebgp-ibgp":
                if role == "spine":
                    node["bgp"] = {"as": SPINE_AS}
                else:
                    index = leaf_index.get(device_name, 1)
                    bgp: dict[str, Any] = {
                        "as": LEAF_OVERLAY_AS,
                        "local_as": LEAF_LOCAL_AS_BASE + index,
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
                        vlan_key = _attr(segment, "name") or f"vlan{vlan_id}"
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
                    links.append(
                        {
                            "interfaces": [
                                {"node": source_name, "ifname": source_if},
                                {"node": remote_name, "ifname": remote_if},
                            ]
                        }
                    )

        host_names: list[str] = []
        if strategy == "ebgp-ibgp":
            for leaf in leafs:
                leaf_name = _attr(leaf, "name")
                if not leaf_name:
                    continue
                attachment: tuple[str, str, dict[str, Any]] | None = None
                for interface_edge in leaf.get("interfaces", {}).get("edges") or []:
                    interface = interface_edge.get("node") or {}
                    ifname = _attr(interface, "name")
                    if not ifname or _attr(interface, "role") in SKIP_INTERFACE_ROLES:
                        continue
                    segment = _segment_on_interface(interface)
                    if segment is None:
                        continue
                    vlan_id = _attr(segment, "vlan_id")
                    if vlan_id is None:
                        continue
                    vlan_key = _attr(segment, "name") or f"vlan{vlan_id}"
                    attachment = (ifname, vlan_key, segment)
                    break
                if attachment is None:
                    continue
                ifname, vlan_key, segment = attachment
                host_name = f"h{len(host_names) + 1}"
                host_names.append(host_name)
                nodes[host_name] = {"device": "linux", "role": "host"}
                host_link: dict[str, Any] = {
                    "vlan.access": vlan_key,
                    "interfaces": [
                        {"node": host_name, "ifname": "eth1"},
                        {"node": leaf_name, "ifname": ifname},
                    ],
                }
                prefix_node = (segment.get("prefix") or {}).get("node") or {}
                prefix = _attr(prefix_node, "prefix")
                if prefix:
                    host_ip = _host_ip(prefix, 10 + len(host_names))
                    if host_ip:
                        host_link["interfaces"][0]["ipv4"] = host_ip
                links.append(host_link)

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

        topology: dict[str, Any] = {
            "_concept": "topology",
            "name": name,
            "device": "frr",
            "module": [],
            "nodes": nodes,
            "links": links,
        }
        if strategy == "ebgp-ibgp":
            topology["module"] = ["vlan", "vxlan", "bgp", "evpn"]
            topology["vxlan"] = {"flooding": "evpn"}
            if vlans:
                topology["vlans"] = vlans
        elif strategy:
            topology["module"] = ["bgp"]

        document = {
            "id": name,
            "title": f"Browser twin: {name}",
            "summary": f"lab.net twin of Infrahub topology {name}",
            "_id": name,
            "topology": topology,
        }
        return f"---\n{yaml.safe_dump(document, sort_keys=False)}---\n"
