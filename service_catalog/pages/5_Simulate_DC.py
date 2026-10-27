"""Simulate a data center fabric in lab.net (browser twin).

Fetches the ``labnet-topology`` artifact from Infrahub, encodes it as a
gzip+base64 fragment, and opens the configured lab.net URL.
"""

from __future__ import annotations

import base64
import gzip
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

import streamlit as st  # type: ignore[import-untyped]
import streamlit.components.v1 as components  # type: ignore[import-untyped]
from utils import (
    DEFAULT_BRANCH,
    INFRAHUB_ADDRESS,
    INFRAHUB_API_TOKEN,
    INFRAHUB_UI_URL,
    LABNET_URL,
    InfrahubClient,
    display_error,
)
from utils.api import InfrahubAPIError

LABNET_ARTIFACT_DEFINITION = "Lab.net Topology"
LABNET_ARTIFACT_NAME = "labnet-topology"

if "selected_branch" not in st.session_state:
    st.session_state.selected_branch = DEFAULT_BRANCH


def labnet_url_for_artifact(topology_xp: str, labnet_url: str = LABNET_URL) -> str:
    """Build a lab.net URL that loads ``topology_xp`` as a gzip+base64 ``#lab=`` artefact.

    Args:
        topology_xp: Raw ``.xp`` document from the Infrahub artifact.
        labnet_url: Workbench or embed base URL.

    Returns:
        Absolute URL with a ``#lab=<artefact>`` fragment.
    """
    parsed = urlparse(labnet_url)
    base = urlunparse(parsed._replace(fragment="", query=""))
    artefact = base64.b64encode(gzip.compress(topology_xp.encode("utf-8"))).decode("ascii")
    return f"{base}#{urlencode({'lab': artefact})}"


def _dc_label(dc: dict[str, Any]) -> str:
    """Return a human-readable datacenter label."""
    name = dc.get("name", {}).get("value") or dc.get("id") or "unknown"
    strategy = dc.get("strategy", {}).get("value")
    return f"{name} ({strategy})" if strategy else str(name)


def main() -> None:
    """Render the Simulate DC page."""
    st.title("Simulate DC")
    st.caption(
        "Open a browser twin of an Infrahub data center in lab.net. "
        f"Default host: `{LABNET_URL}`. Override with `LABNET_URL` "
        "(local embed: `http://127.0.0.1:5500/dist/pages/embed.html`)."
    )

    try:
        client = InfrahubClient(
            base_url=INFRAHUB_ADDRESS,
            api_token=INFRAHUB_API_TOKEN,
            ui_url=INFRAHUB_UI_URL,
        )
    except Exception as exc:
        display_error(f"Could not connect to Infrahub: {exc}")
        st.stop()

    try:
        branches = client.get_branches()
        branch_names: list[str] = [
            str(name) for branch_info in branches if (name := branch_info.get("name") or branch_info.get("id"))
        ]
    except Exception as exc:
        display_error(f"Failed to list branches: {exc}")
        st.stop()

    if not branch_names:
        branch_names = [DEFAULT_BRANCH]

    branch_index = 0
    if st.session_state.selected_branch in branch_names:
        branch_index = branch_names.index(st.session_state.selected_branch)

    selected_branch = st.selectbox("Branch", branch_names, index=branch_index)
    branch = selected_branch if isinstance(selected_branch, str) else DEFAULT_BRANCH
    st.session_state.selected_branch = branch

    try:
        datacenters = client.get_datacenters(branch=branch)
    except Exception as exc:
        display_error(f"Failed to list data centers: {exc}")
        st.stop()

    if not datacenters:
        st.info("No data centers on this branch. Create one first, then return here.")
        st.stop()

    dc_by_label = {_dc_label(dc): dc for dc in datacenters}
    selected_label = st.selectbox("Data center", list(dc_by_label.keys()))
    selected_dc = dc_by_label[selected_label]
    dc_id = selected_dc["id"]
    dc_name = selected_dc.get("name", {}).get("value") or "topology"

    run = st.button("Generate & open lab", type="primary")

    if run:
        with st.status("Preparing lab.net topology…", expanded=True) as status:
            try:
                st.write(f"Generating `{LABNET_ARTIFACT_DEFINITION}`…")
                artifacts = client.generate_and_wait_for_artifacts(
                    dc_id,
                    [LABNET_ARTIFACT_DEFINITION],
                    branch,
                    timeout=180,
                )
                artifact = artifacts[0]
                xp_text = client.get_artifact_content(artifact["storage_id"])
                status.update(label="Topology ready", state="complete")
            except InfrahubAPIError as exc:
                status.update(label="Artifact generation failed", state="error")
                display_error(str(exc))
                st.stop()
            except Exception as exc:
                status.update(label="Artifact generation failed", state="error")
                display_error(f"Unexpected error: {exc}")
                st.stop()

        st.session_state["labnet_xp"] = xp_text
        st.session_state["labnet_url"] = labnet_url_for_artifact(xp_text)
        st.session_state["labnet_dc_name"] = dc_name

    if "labnet_xp" in st.session_state and st.session_state.get("labnet_dc_name") == dc_name:
        lab_url = st.session_state["labnet_url"]
        xp_text = st.session_state["labnet_xp"]

        st.success(f"Browser twin ready for **{dc_name}**.")
        st.link_button("Open in lab.net", lab_url)
        st.download_button(
            "Download .xp",
            data=xp_text,
            file_name=f"{dc_name}.xp",
            mime="text/plain",
        )
        components.iframe(lab_url, height=720, scrolling=True)
    elif not run:
        st.info("Select a data center and click **Generate & open lab**.")


main()
