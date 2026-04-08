from __future__ import annotations

import gzip
import json
import textwrap
import time
from dataclasses import dataclass, field
from typing import Any

import requests
import viktor as vkt


APS_INTEGRATION_NAME = "aps"
APS_BASE_URL = "https://developer.api.autodesk.com"
REQUEST_TIMEOUT = 60

@dataclass
class ViewerState:
    version_urn: str | None = None
    highlight_elements: list[dict[str, str]] = field(default_factory=list)
    diff_summary: dict[str, int] = field(default_factory=dict)
    diff_rows: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {"added": [], "removed": [], "changed": []}
    )


_VIEWER_STATE = ViewerState()


def clear_viewer_state() -> None:
    global _VIEWER_STATE
    _VIEWER_STATE = ViewerState()


def load_viewer_state() -> ViewerState:
    return _VIEWER_STATE


def save_viewer_state(
    *,
    version_urn: str | None,
    highlight_elements: list[dict[str, str]] | None = None,
    diff_summary: dict[str, int] | None = None,
    diff_rows: dict[str, list[dict[str, Any]]] | None = None,
) -> None:
    global _VIEWER_STATE
    _VIEWER_STATE = ViewerState(
        version_urn=version_urn,
        highlight_elements=highlight_elements or [],
        diff_summary=diff_summary or {},
        diff_rows=diff_rows or {"added": [], "removed": [], "changed": []},
    )


@dataclass(frozen=True)
class ModelContext:
    version_urn: str
    token: str


def get_model_context(autodesk_file) -> ModelContext:
    if not autodesk_file:
        raise ValueError("No Autodesk file selected.")

    integration = vkt.external.OAuth2Integration(APS_INTEGRATION_NAME)
    token = integration.get_access_token()

    version = autodesk_file.get_latest_version(token)
    return ModelContext(
        version_urn=version.urn,
        token=token,
    )


@dataclass(frozen=True)
class ModelDiffResult:
    previous_version_urn: str
    current_version_urn: str
    added: list[dict[str, Any]]
    removed: list[dict[str, Any]]
    changed: list[dict[str, Any]]

    @property
    def summary(self) -> dict[str, int]:
        return {
            "added": len(self.added),
            "removed": len(self.removed),
            "changed": len(self.changed),
        }

    @property
    def current_highlight_payload(self) -> list[dict[str, str]]:
        seen: set[str] = set()
        payload: list[dict[str, str]] = []

        for row in self.added:
            external_id = row.get("externalId")
            if external_id and external_id not in seen:
                seen.add(external_id)
                payload.append(
                    {"externalElementId": external_id, "color": "#2e7d32"}
                )

        for row in self.changed:
            external_id = row.get("externalId")
            if external_id and external_id not in seen:
                seen.add(external_id)
                payload.append(
                    {"externalElementId": external_id, "color": "#f57c00"}
                )

        return payload


def _dm_project_id(project_id: str) -> str:
    return project_id if project_id.startswith("b.") else f"b.{project_id}"


def _acc_project_id(project_id: str) -> str:
    return project_id[2:] if project_id.startswith("b.") else project_id


def _get_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, application/vnd.api+json",
    }


def _get_json_headers(token: str) -> dict[str, str]:
    headers = _get_headers(token).copy()
    headers["Content-Type"] = "application/json"
    return headers


def _parse_json_or_ndjson(content: bytes) -> list[dict[str, Any]]:
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)

    text = content.decode("utf-8").strip()
    if not text:
        return []

    if text.startswith("["):
        data = json.loads(text)
        return data if isinstance(data, list) else [data]

    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def list_versions(autodesk_file, token: str) -> list[dict[str, Any]]:
    project_id = _dm_project_id(autodesk_file.project_id)
    item_id = autodesk_file.urn

    url = f"{APS_BASE_URL}/data/v1/projects/{project_id}/items/{item_id}/versions"
    response = requests.get(url, headers=_get_headers(token), timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    data = response.json().get("data", [])
    versions: list[dict[str, Any]] = []
    for item in data:
        attrs = item.get("attributes", {})
        versions.append(
            {
                "version_urn": item["id"],
                "version_number": attrs.get("versionNumber", 0),
                "display_name": attrs.get("displayName") or attrs.get("name") or "",
                "create_time": attrs.get("createTime"),
            }
        )

    versions.sort(key=lambda x: x["version_number"], reverse=True)
    return versions


def start_diff_job(
    project_id: str,
    prev_version_urn: str,
    cur_version_urn: str,
    token: str,
) -> str:
    acc_project_id = _acc_project_id(project_id)
    url = f"{APS_BASE_URL}/construction/index/v2/projects/{acc_project_id}/diffs:batch-status"
    payload = {
        "diffs": [
            {
                "prevVersionUrn": prev_version_urn,
                "curVersionUrn": cur_version_urn,
            }
        ]
    }

    response = requests.post(
        url,
        headers=_get_json_headers(token),
        data=json.dumps(payload),
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    result = response.json()
    diffs = result.get("diffs", [])
    if not diffs:
        raise RuntimeError("APS returned no diff jobs.")

    diff_id = diffs[0].get("diffId")
    if not diff_id:
        raise RuntimeError(f"APS did not return a diffId: {result}")

    return diff_id


def wait_for_diff(
    project_id: str,
    diff_id: str,
    token: str,
    max_wait_seconds: int = 120,
) -> None:
    acc_project_id = _acc_project_id(project_id)
    url = f"{APS_BASE_URL}/construction/index/v2/projects/{acc_project_id}/diffs/{diff_id}"

    started = time.time()
    while True:
        response = requests.get(url, headers=_get_headers(token), timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        result = response.json()

        state = result.get("state")
        if state == "FINISHED":
            return
        if state in {"FAILED", "CANCELLED"}:
            raise RuntimeError(f"Diff job ended with state={state}: {result}")

        if time.time() - started > max_wait_seconds:
            raise TimeoutError(f"Timed out waiting for diff {diff_id}.")

        time.sleep(2)


def download_diff_rows(project_id: str, diff_id: str, token: str) -> list[dict[str, Any]]:
    acc_project_id = _acc_project_id(project_id)
    url = f"{APS_BASE_URL}/construction/index/v2/projects/{acc_project_id}/diffs/{diff_id}/properties"

    response = requests.get(url, headers=_get_headers(token), timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return _parse_json_or_ndjson(response.content)


def get_latest_vs_previous_diff(autodesk_file, token: str) -> ModelDiffResult:
    versions = list_versions(autodesk_file, token)
    if len(versions) < 2:
        raise RuntimeError("The selected ACC file has fewer than 2 versions.")

    current_version_urn = versions[0]["version_urn"]
    previous_version_urn = versions[1]["version_urn"]

    diff_id = start_diff_job(
        project_id=autodesk_file.project_id,
        prev_version_urn=previous_version_urn,
        cur_version_urn=current_version_urn,
        token=token,
    )
    wait_for_diff(autodesk_file.project_id, diff_id, token)
    rows = download_diff_rows(autodesk_file.project_id, diff_id, token)

    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []

    for row in rows:
        row_type = row.get("type")
        if row_type == "OBJECT_ADDED":
            added.append(row)
        elif row_type == "OBJECT_REMOVED":
            removed.append(row)
        else:
            changed.append(row)

    return ModelDiffResult(
        previous_version_urn=previous_version_urn,
        current_version_urn=current_version_urn,
        added=added,
        removed=removed,
        changed=changed,
    )



class Parametrization(vkt.Parametrization):
    intro = vkt.Text(
        textwrap.dedent(
            """
            ## Model Diff Viewer

            Select one Autodesk model and compare the latest version against the previous version.

            What happens:
            - the current version is shown in the viewer
            - added elements are highlighted in green
            - changed elements are highlighted in orange
            - removed elements are stored in the diff result, but cannot be highlighted in the current version
            """
        )
    )

    autodesk_file = vkt.AutodeskFileField(
        "Autodesk model",
        oauth2_integration=APS_INTEGRATION_NAME,
    )

    compare_versions = vkt.ActionButton(
        "Compare latest vs previous",
        method="compare_latest_vs_previous",
    )

    reset_view = vkt.ActionButton(
        "Reset comparison",
        method="reset_comparison",
    )


class Controller(vkt.Controller):
    parametrization = Parametrization(width=35)

    def compare_latest_vs_previous(self, params, **kwargs):
        if not params.autodesk_file:
            raise vkt.UserError("Select an Autodesk model first.")

        integration = vkt.external.OAuth2Integration(APS_INTEGRATION_NAME)
        token = integration.get_access_token()

        try:
            diff = get_latest_vs_previous_diff(params.autodesk_file, token)
        except Exception as exc:
            raise vkt.UserError(f"Could not compare versions: {exc}") from exc

        save_viewer_state(
            version_urn=diff.current_version_urn,
            highlight_elements=diff.current_highlight_payload,
            diff_summary=diff.summary,
            diff_rows={
                "added": diff.added,
                "removed": diff.removed,
                "changed": diff.changed,
            },
        )

        vkt.UserMessage.success(
            (
                f"Compared latest vs previous version. "
                f"Added: {diff.summary['added']}, "
                f"Removed: {diff.summary['removed']}, "
                f"Changed: {diff.summary['changed']}."
            )
        )

    def reset_comparison(self, params, **kwargs):
        if not params.autodesk_file:
            clear_viewer_state()
            vkt.UserMessage.info("Comparison state cleared.")
            return

        try:
            context = get_model_context(params.autodesk_file)
        except Exception as exc:
            raise vkt.UserError(f"Could not reset viewer: {exc}") from exc

        save_viewer_state(
            version_urn=context.version_urn,
            highlight_elements=[],
            diff_summary={},
            diff_rows={"added": [], "removed": [], "changed": []},
        )
        vkt.UserMessage.info("Comparison state cleared.")

    @vkt.WebView("Viewer", duration_guess=30)
    def show_cad_model(self, params, **kwargs) -> vkt.WebResult:
        from aps_viewer_sdk import APSViewer

        if not params.autodesk_file:
            clear_viewer_state()
            return vkt.WebResult(
                html="""
                <div style="padding: 24px; font-family: Arial, sans-serif;">
                    <h3>No Autodesk model selected</h3>
                    <p>Select a model to display it in the viewer.</p>
                </div>
                """
            )

        try:
            context = get_model_context(params.autodesk_file)
        except Exception as exc:
            return vkt.WebResult(
                html=f"""
                <div style="padding: 24px; font-family: Arial, sans-serif;">
                    <h3>Could not load model</h3>
                    <p>{exc}</p>
                </div>
                """
            )

        viewer_state = load_viewer_state()
        version_urn = viewer_state.version_urn or context.version_urn

        viewer = APSViewer(
            urn=version_urn,
            token=context.token,
            views_selector=True,
        )

        if viewer_state.version_urn == version_urn and viewer_state.highlight_elements:
            viewer.highlight_elements(viewer_state.highlight_elements)

        return vkt.WebResult(html=viewer.write())