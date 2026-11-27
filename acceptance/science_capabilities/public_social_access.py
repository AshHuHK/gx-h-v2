"""Check the public publisher access rule for a supplied archived article link, through Clash."""
import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import json
from pathlib import Path
import runpy
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

ROOT = Path(__file__).resolve().parents[2]
IDENTITY = ROOT / "gx-h/plugins/installed/org.gx.scholarly-sources/1.0.0/methods/social.py"
AGENT = "GX-H-public-source/1.0"
MAX_POLICY_BYTES = 256 * 1024


def access(metadata):
    raw = metadata.read_bytes()
    doc = json.loads(raw)
    provider, url, article = runpy.run_path(str(IDENTITY))["article_identity"](doc.get("original_url") or doc.get("url"))
    target = urlsplit(url)
    # The identity adapter accepts only canonical HTTPS publisher article endpoints and strips trackers.
    host = target.hostname
    policy_url = f"https://{host}/robots.txt"
    cx = http.client.HTTPSConnection("127.0.0.1", 7890, timeout=15)
    cx.set_tunnel(host, 443)
    receipt = {"provider": provider, "host": host, "article_identity": article, "article_path": target.path,
               "supplied_metadata_sha256": hashlib.sha256(raw).hexdigest(), "article_body_requested": False,
               "policy_url": policy_url, "proxy": "http://127.0.0.1:7890", "user_agent": AGENT,
               "observed_at": datetime.now(timezone.utc).isoformat()}
    try:
        cx.request("GET", "/robots.txt", headers={"User-Agent": AGENT, "Accept": "text/plain",
                                                   "Accept-Encoding": "identity"})
        response = cx.getresponse()
        data = response.read(MAX_POLICY_BYTES + 1)
        receipt.update(http_status=response.status, media_type=response.getheader("Content-Type"))
        if response.status != 200 or len(data) > MAX_POLICY_BYTES:
            return dict(receipt, disposition="policy_unavailable")
        text = data.decode("utf-8", errors="replace")
        if "user-agent:" not in text.lower():
            return dict(receipt, disposition="policy_unrecognized")
        receipt["policy_sha256"] = hashlib.sha256(data).hexdigest()
        policy = RobotFileParser(url=policy_url)
        policy.parse(text.splitlines())
        if not policy.can_fetch(AGENT, url):
            return dict(receipt, disposition="robots_disallow", usable_article_evidence=False)
        return dict(receipt, disposition="public_article_access_unverified", usable_article_evidence=False)
    finally:
        cx.close()


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("metadata", nargs="+", type=Path, help="PI-provided JSON with url or archived original_url")
    args = cli.parse_args()
    # A transport error stops this batch; no alternate path or credential is attempted.
    print(json.dumps({"schema": "gx.public-social-access/1", "cases": [access(m) for m in args.metadata]},
                     indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
