"""Synthetic HTTP, security-header, API, and static-asset tests for the loopback workbench UI."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path

import pytest

from pocketfinancer_sms.analyzer import DeterministicSmsAnalyzer
from pocketfinancer_sms.corpus.grouping import build_grouping
from pocketfinancer_sms.currency import CurrencyContext
from pocketfinancer_sms.extractor import SourceSpan
from pocketfinancer_sms.workbench.service import WorkbenchService
from pocketfinancer_sms.workbench.store import WorkbenchStore
from pocketfinancer_sms.workbench.web import ASSET_ROOT, WorkbenchWebServer


BODY = "INR 15 was debited from account **1234 at SYNTH STORE."


def _server(tmp_path: Path, *, pool: str = "protected_test") -> tuple[WorkbenchWebServer, str]:
    source_id = "src_" + "b" * 32
    analysis = DeterministicSmsAnalyzer(
        CurrencyContext("INR", ("core-en", "india"))
    ).analyze(BODY, operation_id=source_id, is_outgoing=False)
    grouping = build_grouping(b"web-synthetic-key" * 2, BODY, "SYNTH-BANK", "2024-02-01T00:00:00Z")
    record = {
        "contract": "pocketfinancer.corpus-record/1",
        "source_id": source_id,
        "source": {"body": BODY, "sender": "SYNTH-BANK"},
        "source_metadata": {
            "source_record_id": "synthetic-web",
            "source_row_index": 0,
            "timestamp": "2024-02-01T00:00:00Z",
            "service": "SMS",
            "is_outgoing": False,
        },
        "analysis": analysis.to_dict(),
        "weak_facets": {
            "disposition": "invoke",
            "selector_action": "run_normal",
            "operational_class": "posted_candidate",
            "event_state": "posted",
            "financial_family": "card_purchase",
            "payment_rail": "card",
            "confidence": "medium",
            "reason_codes": ["amount_and_direction_present"],
        },
        "grouping": asdict(grouping),
        "pool": pool,
        "review_state": "unreviewed",
        "provenance": {"corpus_run_id": "synthetic-web-run"},
    }
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(json.dumps(record) + "\n", encoding="utf-8")
    store = WorkbenchStore(tmp_path / "private" / "workbench.sqlite3")
    store.import_manifest(manifest, corpus_run_id="synthetic-web-run")
    server = WorkbenchWebServer(
        WorkbenchService(store),
        port=0,
        token="synthetic-token",
        backup_root=tmp_path / "backups",
        export_root=tmp_path / "exports",
    )
    return server, source_id


def _request(
    server: WorkbenchWebServer,
    path: str,
    *,
    authorized: bool = True,
    origin: str | None = None,
    content_type: str = "application/json",
    payload: dict | None = None,
) -> urllib.request.Request:
    headers = {}
    if authorized:
        headers["X-Workbench-Token"] = server.token
        headers["Origin"] = origin if origin is not None else f"http://127.0.0.1:{server.port}"
    data = None
    if payload is not None:
        headers["Content-Type"] = content_type
        data = json.dumps(payload).encode("utf-8")
    return urllib.request.Request(
        f"http://127.0.0.1:{server.port}{path}",
        data=data,
        headers=headers,
        method="POST" if payload is not None else "GET",
    )


def _v2_posted_payload() -> dict:
    def span(text: str) -> dict:
        start = BODY.index(text)
        return SourceSpan.from_source(BODY, start, start + len(text)).to_dict()

    return {
        "contract": "pocketfinancer.canonical-label/2",
        "decision": "posted",
        "operational_class": "posted_candidate",
        "event_state": "posted",
        "financial_family": "merchant_payment",
        "payment_rail": "unknown",
        "event": {
            "amount_value": "15",
            "currency": "INR",
            "amount_span": span("INR 15"),
            "direction": "debit",
            "direction_span": span("debited"),
            "account_reference": "**1234",
            "account_span": span("**1234"),
            "existing_account_id": None,
            "counterparty": "SYNTH STORE",
            "counterparty_span": span("SYNTH STORE"),
        },
        "uncertain": False,
        "notes": "",
    }


def test_workbench_static_assets_keep_viewport_layout_and_filter_controls() -> None:
    markup = (ASSET_ROOT / "index.html").read_text(encoding="utf-8")
    styles = (ASSET_ROOT / "styles.css").read_text(encoding="utf-8")
    script = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")

    assert "html, body { height: 100%; }" in styles
    assert "[hidden] { display: none !important; }" in styles
    assert "grid-template-rows: auto auto auto minmax(0, 1fr)" in styles
    assert ".workspace { min-height: 0;" in styles
    assert 'el("detailContent").closest(".detail").scrollTop = 0;' in script
    assert 'id="coverageToggleHint"' in markup
    assert 'coverage.textContent = error.message || "Coverage data could not be loaded.";' in script
    assert 'el("coveragePanel").addEventListener("toggle"' in script
    assert 'id="sourceSetFilter"' in markup
    assert 'source_set: el("sourceSetFilter").value' in script
    assert 'el("sourceSetFilter").addEventListener("change"' in script
    assert "await loadSourceSets();" in script


def test_non_loopback_bind_is_rejected(tmp_path: Path) -> None:
    store = WorkbenchStore(tmp_path / "private" / "workbench.sqlite3")
    with pytest.raises(ValueError, match="127.0.0.1"):
        WorkbenchWebServer(WorkbenchService(store), host="0.0.0.0", port=0)


def test_ui_security_headers_auth_csrf_assets_and_blind_first_flow(tmp_path: Path) -> None:
    server, source_id = _server(tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # Root without token fails closed with 401.
        with pytest.raises(urllib.error.HTTPError) as missing_tok:
            urllib.request.urlopen(f"http://127.0.0.1:{server.port}/")
        assert missing_tok.value.code == 401

        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.port}/?token={server.token}"
        ) as response:
            index = response.read().decode("utf-8")
            assert "PocketFinancer SMS Review" in index
            assert "canonical-label/2" in index
            assert "default-src 'self'" in response.headers["Content-Security-Policy"]
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["Referrer-Policy"] == "no-referrer"
            assert response.headers["X-Frame-Options"] == "DENY"
            assert response.headers["Cache-Control"] == "no-store"
            assert "https://" not in index

        # Allowed assets load; unknown assets return 404.
        for asset in ("/assets/styles.css", "/assets/app.js"):
            with urllib.request.urlopen(f"http://127.0.0.1:{server.port}{asset}") as resp:
                assert resp.status == 200 and resp.read()
        with pytest.raises(urllib.error.HTTPError) as not_found:
            urllib.request.urlopen(f"http://127.0.0.1:{server.port}/assets/secret.txt")
        assert not_found.value.code == 404

        # Unauthorized token and mismatched Origin fail with 401.
        with pytest.raises(urllib.error.HTTPError) as unauth:
            urllib.request.urlopen(
                _request(server, "/api/progress?reviewer_id=synthetic-reviewer", authorized=False)
            )
        assert unauth.value.code == 401

        with pytest.raises(urllib.error.HTTPError) as csrf:
            urllib.request.urlopen(
                _request(
                    server,
                    "/api/progress?reviewer_id=synthetic-reviewer",
                    origin="http://evil.invalid",
                )
            )
        assert csrf.value.code == 401

        # Wrong Content-Type returns 400; unknown API endpoint returns 404.
        with pytest.raises(urllib.error.HTTPError) as bad_ct:
            urllib.request.urlopen(
                _request(server, "/api/draft", content_type="text/plain", payload={"x": 1})
            )
        assert bad_ct.value.code == 400
        with pytest.raises(urllib.error.HTTPError) as unknown_api:
            urllib.request.urlopen(_request(server, "/api/does-not-exist?reviewer_id=r1"))
        assert unknown_api.value.code == 404

        with urllib.request.urlopen(
            _request(server, "/api/progress?reviewer_id=synthetic-reviewer")
        ) as resp:
            assert json.loads(resp.read())["total"] == 1

        with urllib.request.urlopen(
            _request(server, "/api/rows?reviewer_id=synthetic-reviewer")
        ) as resp:
            queue = json.loads(resp.read())
            assert queue["total"] == 1
            assert queue["rows"][0]["blind_locked"] is True

        with urllib.request.urlopen(
            _request(server, f"/api/row?reviewer_id=synthetic-reviewer&source_id={source_id}")
        ) as resp:
            row = json.loads(resp.read())
            assert row["blind_locked"] is True and "analysis" not in row

        with urllib.request.urlopen(
            _request(
                server,
                "/api/resume",
                payload={
                    "reviewer_id": "synthetic-reviewer",
                    "source_id": source_id,
                    "offset": 0,
                    "filters": {"pool": "protected_test"},
                },
            )
        ) as resp:
            assert json.loads(resp.read())["source_id"] == source_id
        with urllib.request.urlopen(
            _request(server, "/api/resume?reviewer_id=synthetic-reviewer")
        ) as resp:
            assert json.loads(resp.read())["filters"]["pool"] == "protected_test"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_v2_http_annotation_conflict_reveal_preview_backup_and_export(tmp_path: Path) -> None:
    server, source_id = _server(tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    reviewer = "synthetic-reviewer"
    try:
        with urllib.request.urlopen(
            _request(
                server,
                "/api/draft",
                payload={
                    "source_id": source_id,
                    "reviewer_id": reviewer,
                    "expected_revision": 0,
                    "payload": {"contract": "pocketfinancer.canonical-label/2", "decision": "posted"},
                },
            )
        ) as resp:
            assert json.loads(resp.read())["revision"] == 1

        # Stale revision returns HTTP 409 Conflict.
        with pytest.raises(urllib.error.HTTPError) as conflict:
            urllib.request.urlopen(
                _request(
                    server,
                    "/api/draft",
                    payload={
                        "source_id": source_id,
                        "reviewer_id": reviewer,
                        "expected_revision": 0,
                        "payload": {"contract": "pocketfinancer.canonical-label/2", "decision": "none"},
                    },
                )
            )
        assert conflict.value.code == 409

        with urllib.request.urlopen(
            _request(
                server,
                "/api/submit",
                payload={
                    "source_id": source_id,
                    "reviewer_id": reviewer,
                    "expected_revision": 1,
                    "payload": _v2_posted_payload(),
                },
            )
        ) as resp:
            submitted = json.loads(resp.read())
            assert submitted["revision"] == 2

        preview_path = f"/api/preview?reviewer_id={reviewer}&source_id={source_id}"
        with pytest.raises(urllib.error.HTTPError) as blind_preview:
            urllib.request.urlopen(_request(server, preview_path))
        assert blind_preview.value.code == 400

        with urllib.request.urlopen(
            _request(server, "/api/reveal", payload={"source_id": source_id, "reviewer_id": reviewer})
        ) as resp:
            assert json.loads(resp.read())["blind_locked"] is False

        with urllib.request.urlopen(_request(server, preview_path)) as resp:
            preview = json.loads(resp.read())
            assert preview["target_contract"] == "pocketfinancer.sms-extractor/1"
            assert preview["target"]["decision"] == "posted"

        with urllib.request.urlopen(_request(server, "/api/backup", payload={})) as resp:
            assert json.loads(resp.read())["corpus_run_id"] == "synthetic-web-run"

        with pytest.raises(urllib.error.HTTPError) as empty_export:
            urllib.request.urlopen(
                _request(
                    server,
                    "/api/export",
                    payload={"explicit_consent": True, "selected_revisions": []},
                )
            )
        assert empty_export.value.code == 400

        selection = [
            {
                "source_id": source_id,
                "reviewer_id": reviewer,
                "revision": submitted["revision"],
                "revision_hash": submitted["revision_hash"],
            }
        ]
        with urllib.request.urlopen(
            _request(
                server,
                "/api/export",
                payload={"explicit_consent": True, "selected_revisions": selection},
            )
        ) as resp:
            assert json.loads(resp.read())["label_count"] == 1
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_http_source_sets_weak_correction_and_adjudication_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, source_id = _server(tmp_path, pool="annotation_development")
    monkeypatch.setattr(
        server.service,
        "list_source_sets",
        lambda: [{"id": "synthetic-set", "name": "Synthetic review set", "count": 1}],
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        path = "/api/source-sets?reviewer_id=synthetic-reviewer"
        with pytest.raises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(_request(server, path, authorized=False))
        assert rejected.value.code == 401
        with urllib.request.urlopen(_request(server, path)) as resp:
            assert json.loads(resp.read()) == [
                {"id": "synthetic-set", "name": "Synthetic review set", "count": 1}
            ]

        with pytest.raises(urllib.error.HTTPError) as bad_set:
            urllib.request.urlopen(
                _request(server, "/api/rows?reviewer_id=synthetic-reviewer&source_set=unknown-set")
            )
        assert bad_set.value.code == 400

        # Weak facet correction via HTTP.
        with urllib.request.urlopen(
            _request(
                server,
                "/api/correction",
                payload={
                    "source_id": source_id,
                    "reviewer_id": "reviewer-a",
                    "expected_revision": 0,
                    "facets": {
                        "operational_class": "ambiguous",
                        "event_state": "unknown",
                        "financial_family": "unknown",
                        "payment_rail": "unknown",
                        "reason": "synthetic http correction",
                    },
                },
            )
        ) as resp:
            assert json.loads(resp.read())["revision"] == 1

        # Two disagreeing submissions followed by adjudication via HTTP.
        with urllib.request.urlopen(
            _request(
                server,
                "/api/submit",
                payload={
                    "source_id": source_id,
                    "reviewer_id": "reviewer-a",
                    "expected_revision": 0,
                    "payload": _v2_posted_payload(),
                },
            )
        ):
            pass
        none_v2 = {
            "contract": "pocketfinancer.canonical-label/2",
            "decision": "none",
            "operational_class": "non_financial",
            "event_state": "no_event",
            "financial_family": None,
            "payment_rail": None,
            "event": None,
            "uncertain": False,
            "notes": "",
        }
        with urllib.request.urlopen(
            _request(
                server,
                "/api/submit",
                payload={
                    "source_id": source_id,
                    "reviewer_id": "reviewer-b",
                    "expected_revision": 0,
                    "payload": none_v2,
                },
            )
        ):
            pass

        with urllib.request.urlopen(
            _request(server, f"/api/disagreements?reviewer_id=adjudicator&source_id={source_id}")
        ) as resp:
            assert json.loads(resp.read())["has_disagreement"] is True

        with urllib.request.urlopen(
            _request(
                server,
                "/api/adjudicate",
                payload={
                    "source_id": source_id,
                    "reviewer_id": "adjudicator",
                    "expected_revision": 0,
                    "payload": _v2_posted_payload(),
                },
            )
        ) as resp:
            assert json.loads(resp.read())["status"] == "adjudicated"
    finally:
        server.shutdown()
        thread.join(timeout=2)
