"""Recover the fixed public A2 inputs by exact digest. All external requests go through Clash."""
import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import socket
import urllib.request
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work", type=Path)
    args = parser.parse_args()
    dest = args.work.resolve() / "materials"
    dest.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((HERE / "materials.json").read_text())
    missing = [item for item in manifest["materials"] if not (dest / item["name"]).is_file() or
               hashlib.sha256((dest / item["name"]).read_bytes()).hexdigest() != item["sha256"]]
    if not missing:
        print(json.dumps({"verified":[i["name"] for i in manifest["materials"]]}))
        return
    proxy = "http://127.0.0.1:7890"
    for port in (7890, 7891):
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
    for key, value in {"http_proxy":proxy, "https_proxy":proxy, "all_proxy":"socks5h://127.0.0.1:7891",
                       "no_proxy":"localhost,127.0.0.1,::1"}.items():
        os.environ.update({key:value,key.upper():value})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http":proxy,"https":proxy}))
    for item in missing:
        path = dest / item["name"]
        with opener.open(item["url"], timeout=30) as resp:
            raw = resp.read()
        if path.suffix == ".xml":
            envelope = ET.fromstring(raw)
            article = envelope.find(".//article")
            if envelope.tag != "article" and article is None:
                raise ValueError("NCBI did not return a JATS article")
            data = raw if envelope.tag == "article" else ET.tostring(article, encoding="utf-8")
        else:
            crossref = json.loads(raw)["message"]
            abstract = html.unescape(re.sub("<[^>]*>", "", crossref["abstract"])).strip()
            data = f"Crossref publisher record\n{crossref['title'][0]}\nDOI {crossref['DOI']}\n\n{abstract}\n".encode()
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError(f"Upstream content or XML rendering changed for {path.name}. Keep this version separate.")
        path.write_bytes(data)
    print(json.dumps({"verified":[i["name"] for i in manifest["materials"]]}))


if __name__ == "__main__":
    main()
