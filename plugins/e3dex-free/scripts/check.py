"""Exercise the actual manifest command; optionally execute a read-only probe."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("E3D_BRIDGE_DATA_DIR", str(Path(os.environ["LOCALAPPDATA"]) / "E3DBridge"))

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id")
    parser.add_argument("--project")
    parser.add_argument("--run-read-only", action="store_true")
    args = parser.parse_args(argv)
    if args.run_read_only and not args.session_id:
        parser.error("--run-read-only requires --session-id")
    out = Path(os.environ["E3D_BRIDGE_DATA_DIR"]) / "diagnostics" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out.mkdir(parents=True)
    results = []
    config = json.loads((ROOT / ".mcp.json").read_text())["mcpServers"]["e3d_sessions"]
    command = [config["command"]] + [s.replace("${PLUGIN_ROOT}", str(ROOT)) for s in config["args"]]
    env = {**os.environ, **config.get("env", {})}
    def rpc(method, params=None):
        messages = [
            dict(jsonrpc="2.0", id=1, method="initialize", params={"protocolVersion":"2025-06-18"}),
            dict(jsonrpc="2.0", method="notifications/initialized"),
            dict(jsonrpc="2.0", id=2, method=method, params=params or {}),
        ]
        process = subprocess.run(command, cwd=str(ROOT), env=env,
            input="".join(json.dumps(m)+"\n" for m in messages), capture_output=True,
            text=True, encoding="utf-8", timeout=210)
        if process.returncode:
            raise RuntimeError("MCP launcher failed: " + process.stderr)
        replies = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        init = next(x for x in replies if x.get("id") == 1)
        response = next(x for x in replies if x.get("id") == 2)
        assert init["result"]["serverInfo"]["name"] == "e3dex"
        results.append({"method":method, "initialization":init, "response":response})
        (out / "responses.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        return response["result"]

    def call(name, arguments):
        result = rpc("tools/call", {"name":name, "arguments":arguments})["structuredContent"]
        if not result["ok"]:
            raise RuntimeError(json.dumps(result))
        return result["data"]

    summary = {"status":"FAILED", "recipient_upload_ui_verified":False}
    try:
        checksums = json.loads((ROOT / "SHA256SUMS.json").read_text())["files"]
        for relative, digest in checksums.items():
            assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == digest, relative
        summary["integrity_files"] = len(checksums)
        names = {t["name"] for t in rpc("tools/list")["tools"]}
        expected=set(json.loads((ROOT/'docs/api-contract.json').read_text(encoding='utf-8'))['operations'])
        expected|={'e3d_account','e3d_guide'}
        assert names == expected, {'missing':sorted(expected-names),'unexpected':sorted(names-expected)}
        summary['mcp_operations']=sorted(names)
        listing = call("e3d_list_sessions", {})
        live = [s for s in listing["sessions"] if s.get("available")]
        summary["live_sessions"] = len(live)
        if not live:
            raise RuntimeError("MCP works, but no available E3D session. Check installation and user-selected project login.")
        if args.session_id:
            assert any(s["session"]["session_id"] == args.session_id for s in live), "Selected session unavailable"
            before = call("e3d_session_context", {"session_id":args.session_id})["context"]
            if args.project: assert before['project']['name']==args.project,'Requested project does not match selected session'
            assert before.get("bridge", {}).get("engine") == "e3d-native", "Independent native bridge required"
            summary["session_id"] = args.session_id
            if args.run_read_only:
                ref = before["currentElement"]["ref"]
                data = call("e3d_run_macro", {
                    "session_id":args.session_id, "macro":str(ROOT / "components/harness/bridge/multi/probe.pmlmac"),
                    "project":before["project"]["name"], "mdb":before["mdb"]["name"], "ref":ref,
                    "effect":"read-only", "confirm":True, "expect":["probe_ref="+ref]})
                report = json.loads(Path(data["report"]["report"]).with_suffix(".json").read_text())
                assert report["status"] == "OK" and not report["issues"], report
                assert all(x["exit_code"] == 0 for x in report["lint"])
                assert report["receipt"]["START_REF"] == report["receipt"]["END_REF"] == [ref]
                assert "probe_ref="+ref in report["receipt"]["DATA"]
                after = call("e3d_session_context", {"session_id":args.session_id})["context"]
                assert after["currentElement"]["ref"] == ref
                assert after["project"]["name"] == before["project"]["name"]
                assert after["mdb"]["name"] == before["mdb"]["name"]
                summary["macro_report"] = report
                summary["after"] = after
        summary["status"] = "OK"
    except Exception as exc:
        summary["error"] = str(exc)
        summary["retry"] = "Do not automatically retry a timed-out macro. Inspect recorded results and runs."
    summary["evidence"] = str(out)
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "OK" else 2

if __name__ == "__main__":
    raise SystemExit(main())

