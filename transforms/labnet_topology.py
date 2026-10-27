"""Transform a TopologyDataCenter into a lab.net .xp topology document."""

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
SPINE_AS = 65100
LEAF_OVERLAY_AS = 65000
LEAF_LOCAL_AS_BASE = 65010
VNI_OFFSET = 10000
DEMO_VLAN = "demo"
DEMO_VLAN_ID = 100
DEMO_VLAN_PREFIX = "192.168.100.0/24"
DEMO_HOST_IF = "Ethernet100"


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
            # lab.net YANG only allows router|host|switch|bridge (not fabric roles).
            platform = _attr((device.get("platform") or {}).get("node"), "nornir_platform")
            if platform not in PLATFORM_DEVICES:
                raise ValueError(f"Unsupported lab.net platform {platform!r} for {device_name}")
            node: dict[str, Any] = {"device": PLATFORM_DEVICES[platform], "role": "router"}
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
        if strategy == "ebgp-ibgp" and len(leafs) >= 1:
            used_ports = {
                (endpoint["node"], endpoint["ifname"]) for link in links for endpoint in link.get("interfaces") or []
            }
            host_names = _attach_demo_hosts(
                leafs=leafs,
                nodes=nodes,
                links=links,
                vlans=vlans,
                used_ports=used_ports,
            )

        topology: dict[str, Any] = {
            "_concept": "topology",
            "name": name,
            "module": [],
            "nodes": nodes,
            "links": links,
        }
        if strategy == "ebgp-ibgp":
            topology["module"] = ["vlan", "vxlan", "bgp", "evpn"]
            topology["vxlan"] = {"flooding": "evpn"}
            if vlans:
                topology["vlans"] = vlans
            if len(host_names) >= 2:
                topology["validate"] = _demo_validate_steps(
                    host_names=host_names,
                    leafs=leafs,
                    spines=[device for device in fabric_devices if _attr(device, "role") == "spine"],
                )
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


def _free_leaf_port(leaf: dict[str, Any], used_ports: set[tuple[str, str]]) -> str | None:
    """Return an unused leaf interface name, inventing ``Ethernet100+`` if needed."""
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
        cable = (interface.get("connector") or {}).get("node")
        if cable is None or _segment_on_interface(interface) is not None:
            return ifname
    for interface_edge in leaf.get("interfaces", {}).get("edges") or []:
        interface = interface_edge.get("node") or {}
        ifname = _attr(interface, "name")
        if ifname and _attr(interface, "role") not in SKIP_INTERFACE_ROLES and (leaf_name, ifname) not in used_ports:
            return ifname
    for index in range(100, 110):
        ifname = f"Ethernet{index}"
        if (leaf_name, ifname) not in used_ports:
            return ifname
    return None


def _segment_attachment(
    leaf: dict[str, Any],
    used_ports: set[tuple[str, str]],
) -> tuple[str, str, int, str] | None:
    """Return ``(ifname, vlan_key, vlan_id, prefix)`` for a free segment-backed port."""
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
        if vlan_id is None:
            continue
        vlan_key = _attr(segment, "name") or f"vlan{vlan_id}"
        prefix_node = (segment.get("prefix") or {}).get("node") or {}
        prefix = _attr(prefix_node, "prefix") or DEMO_VLAN_PREFIX
        return ifname, vlan_key, int(vlan_id), str(prefix)
    return None


def _ensure_vlan(
    vlans: dict[str, Any],
    vlan_key: str,
    *,
    vlan_id: int,
    prefix: str,
) -> None:
    """Register a bridge VLAN/VNI once."""
    if vlan_key in vlans:
        return
    vlans[vlan_key] = {
        "id": vlan_id,
        "mode": "bridge",
        "vni": vlan_id + VNI_OFFSET,
        "prefix": {"ipv4": prefix},
    }


def _attach_demo_hosts(
    *,
    leafs: list[dict[str, Any]],
    nodes: dict[str, Any],
    links: list[dict[str, Any]],
    vlans: dict[str, Any],
    used_ports: set[tuple[str, str]],
) -> list[str]:
    """Attach two linux hosts on a shared VLAN (prefer distinct leafs).

    Prefers Infrahub segment attachments; otherwise synthesizes a ``demo`` VLAN
    and free/invented leaf ports so the browser twin always has a ping story.
    """
    host_names: list[str] = []
    # One host on each of the first two leafs; duplicate the only leaf when alone.
    targets = leafs[:2] if len(leafs) >= 2 else [leafs[0], leafs[0]]

    shared_vlan: str | None = None
    shared_prefix: str | None = None
    for leaf in targets:
        leaf_name = _attr(leaf, "name")
        if not leaf_name:
            continue

        attachment = _segment_attachment(leaf, used_ports)
        ifname: str
        vlan_key: str
        prefix: str
        if attachment is not None and shared_vlan is None:
            ifname, vlan_key, vlan_id, prefix = attachment
            _ensure_vlan(vlans, vlan_key, vlan_id=vlan_id, prefix=prefix)
        else:
            free_port = _free_leaf_port(leaf, used_ports)
            if free_port is None:
                continue
            ifname = free_port
            if shared_vlan is None:
                vlan_key = DEMO_VLAN
                prefix = DEMO_VLAN_PREFIX
                _ensure_vlan(vlans, vlan_key, vlan_id=DEMO_VLAN_ID, prefix=prefix)
            else:
                vlan_key = shared_vlan
                prefix = shared_prefix or DEMO_VLAN_PREFIX

        if shared_vlan is None:
            shared_vlan = vlan_key
            shared_prefix = prefix
        else:
            vlan_key = shared_vlan
            prefix = shared_prefix or DEMO_VLAN_PREFIX

        host_name = f"h{len(host_names) + 1}"
        host_names.append(host_name)
        nodes[host_name] = {"device": "linux", "role": "host"}
        host_ip = _host_ip(prefix, 9 + len(host_names))
        host_endpoint: dict[str, Any] = {"node": host_name, "ifname": "eth1"}
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


def _demo_validate_steps(
    *,
    host_names: list[str],
    leafs: list[dict[str, Any]],
    spines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build topology.validate steps: BGP up, then cross-host ICMP."""
    steps: list[dict[str, Any]] = []
    spine_name = _attr(spines[0], "name") if spines else None
    leaf_name = _attr(leafs[0], "name") if leafs else None
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
