import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import uvicorn
from fastapi import FastAPI, Request
import urllib.request
import urllib.parse

app = FastAPI(title="Order Tracker Incident Responder")
BASE_DIR = Path(__file__).resolve().parent
REPO_DIR = BASE_DIR.parent
INCIDENTS_DIR = BASE_DIR / "incidents"
INCIDENTS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("incident-responder")


def fetch_json(url: str, timeout: int = 3):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "IncidentResponder/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}


def collect_evidence(endpoint: str = "") -> Dict[str, Any]:
    evidence = {}
    
    # 1. Prometheus 5xx metrics
    prom_url = "http://localhost:9090/api/v1/query?" + urllib.parse.urlencode(
        {"query": 'http_requests_total{status_code=~"5.."}'}
    )
    evidence["prometheus_5xx"] = fetch_json(prom_url)

    # 2. Loki logs for order-tracker
    loki_url = "http://localhost:3100/loki/api/v1/query_range?" + urllib.parse.urlencode(
        {"query": '{service_name="order-tracker"}', "limit": 20}
    )
    evidence["loki_logs"] = fetch_json(loki_url)

    # 3. Tempo traces
    tempo_url = "http://localhost:3200/api/search?limit=10"
    evidence["tempo_traces"] = fetch_json(tempo_url)

    return evidence


@app.post("/alerts")
async def receive_alert(request: Request):
    payload = await request.json()
    incident_id = datetime.now(timezone.utc).strftime("incident-%Y%m%d-%H%M%S")
    incident_dir = INCIDENTS_DIR / incident_id
    incident_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Received alert notification. Created incident ID: {incident_id}")

    # Save incoming alert payload
    alert_file = incident_dir / "alert.json"
    with open(alert_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    # Extract first alert info if available
    alerts = payload.get("alerts", [{}])
    first_alert = alerts[0] if alerts else {}
    labels = first_alert.get("labels", {})
    annotations = first_alert.get("annotations", {})
    status = first_alert.get("status", "unknown")
    alert_name = labels.get("alertname", "UnknownAlert")
    endpoint = annotations.get("endpoint", labels.get("route", ""))

    # Collect operational evidence
    evidence = collect_evidence(endpoint)
    evidence_file = incident_dir / "evidence.json"
    with open(evidence_file, "w", encoding="utf-8") as f:
        json.dump(evidence, f, indent=2)

    # Check if test alert
    is_test = labels.get("test") == "true" or alert_name == "ResponderTest"

    # Construct prompt for the headless coding agent
    prompt = f"""You are an automated on-call incident responder for Order Tracker.
An alert was received:
- Incident ID: {incident_id}
- Alert Name: {alert_name}
- Status: {status}
- Labels: {json.dumps(labels, indent=2)}
- Annotations: {json.dumps(annotations, indent=2)}

Evidence summary:
- Endpoint: {endpoint}
- Evidence file: {evidence_file}

Instructions:
1. If this is a test notification (e.g. alertname is 'ResponderTest' or test is 'true'), acknowledge receipt, confirm that the responder pipeline is healthy, and report that no incident fix is required.
2. If this is a real incident (e.g. 5xx errors on an endpoint), investigate the source code in {REPO_DIR}, find the root cause of the error, fix the bug in the code, run pytest to ensure tests pass, and report what the problem was and what changes were made.
3. Provide your final diagnosis and concluding response.
"""

    logger.info(f"Invoking headless coding assistant (agy) for incident {incident_id}...")
    agy_path = "/home/yami/.local/bin/agy"
    if not Path(agy_path).exists():
        agy_path = "agy"

    cmd = [
        agy_path,
        "-p",
        prompt,
        "--dangerously-skip-permissions",
    ]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_DIR),
            capture_output=True,
            text=True,
            timeout=180,
        )
        agent_response = proc.stdout if proc.returncode == 0 else (proc.stdout + "\n" + proc.stderr)
    except subprocess.TimeoutExpired:
        agent_response = "Error: Agent invocation timed out after 180 seconds."
    except Exception as e:
        agent_response = f"Error running agent: {str(e)}"

    logger.info(f"Agent finished. Storing response for incident {incident_id}")

    # Store agent response
    response_file = incident_dir / "agent_response.txt"
    with open(response_file, "w", encoding="utf-8") as f:
        f.write(agent_response)

    latest_file = BASE_DIR / "latest_response.txt"
    with open(latest_file, "w", encoding="utf-8") as f:
        f.write(agent_response)

    if not is_test:
        logger.info("Rebuilding and restarting app container to apply the fix...")
        subprocess.run(["docker", "compose", "up", "--build", "-d", "--wait", "app"], cwd=str(REPO_DIR))
        verify_cmd = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "http://localhost:8000/api/orders/express-1002"], capture_output=True, text=True)
        logger.info(f"Verification HTTP status code: {verify_cmd.stdout.strip()}")

    print("\n--- AGENT RESPONSE START ---")
    print(agent_response)
    print("--- AGENT RESPONSE END ---\n")

    return {
        "status": "ok",
        "incident_id": incident_id,
        "alert_name": alert_name,
        "agent_response": agent_response.strip(),
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
