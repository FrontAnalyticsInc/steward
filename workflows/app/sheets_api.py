"""Deterministic Google Sheets access for workflow pipelines.

This module is deliberately not exposed to model stages. Workflows call it from
BaseAgent stages after extraction/validation has already happened.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Sequence

from app import integration_log

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SERVICE_ACCOUNT_FILE = "GOOGLE_SHEETS_SERVICE_ACCOUNT_FILE"
SPREADSHEET_ID = "LINKEDIN_JOB_ALERTS_SPREADSHEET_ID"


def configured() -> bool:
    return bool(os.environ.get(SERVICE_ACCOUNT_FILE) and os.environ.get(SPREADSHEET_ID))


def missing_config() -> list[str]:
    missing = []
    if not os.environ.get(SERVICE_ACCOUNT_FILE):
        missing.append(SERVICE_ACCOUNT_FILE)
    if not os.environ.get(SPREADSHEET_ID):
        missing.append(SPREADSHEET_ID)
    return missing


def _credentials():
    sa_file = os.environ.get(SERVICE_ACCOUNT_FILE)
    if not sa_file:
        raise RuntimeError(f"no Google Sheets credential: set {SERVICE_ACCOUNT_FILE}")
    from google.oauth2 import service_account

    return service_account.Credentials.from_service_account_file(sa_file, scopes=SCOPES)


def build_service():
    from googleapiclient.discovery import build

    return build("sheets", "v4", credentials=_credentials(), cache_discovery=False)


def _execute(call, what: str) -> Any:
    try:
        result = call().execute()
    except Exception as exc:  # noqa: BLE001
        integration_log.record(source="sheets", operation=what, ok=False, capability="write", error=exc)
        raise
    integration_log.record(source="sheets", operation=what, ok=True, capability="write")
    return result


def read_values(service, spreadsheet_id: str, range_name: str) -> list[list[Any]]:
    response = _execute(
        lambda: service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_name,
        ),
        f"values.get {range_name}",
    )
    return response.get("values", [])


def append_values(
    service,
    spreadsheet_id: str,
    range_name: str,
    rows: Sequence[Sequence[Any]],
) -> None:
    if not rows:
        return
    _execute(
        lambda: service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [list(row) for row in rows]},
        ),
        f"values.append {range_name}",
    )


def update_values(
    service,
    spreadsheet_id: str,
    range_name: str,
    rows: Sequence[Sequence[Any]],
) -> None:
    _execute(
        lambda: service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=range_name,
            valueInputOption="RAW",
            body={"values": [list(row) for row in rows]},
        ),
        f"values.update {range_name}",
    )


def sheet_titles(service, spreadsheet_id: str) -> set[str]:
    response = _execute(
        lambda: service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties.title",
        ),
        "spreadsheets.get titles",
    )
    return {
        ((sheet.get("properties") or {}).get("title") or "")
        for sheet in response.get("sheets", [])
        if (sheet.get("properties") or {}).get("title")
    }


def add_sheets(service, spreadsheet_id: str, titles: Sequence[str]) -> None:
    requests = [{"addSheet": {"properties": {"title": title}}} for title in titles]
    if not requests:
        return
    _execute(
        lambda: service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ),
        "spreadsheets.batchUpdate addSheet",
    )
