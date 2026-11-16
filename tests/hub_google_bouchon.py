"""Harnais de test : lance le vrai hub (uvicorn, rosetta.main:app) avec Google bouchonné.

Seule la transport de l'addon maps est remplacée. La réponse de Google est lue à chaque requête
dans le fichier JSON `BOUCHON_SCENARIO` ({"status": int, "payload": {...}}) ; la requête reçue
est consignée dans `BOUCHON_JOURNAL` (JSON : masque et corps).
"""
import json
import os
import sys

import httpx
import uvicorn

from rosetta.addons import maps


def handler(request: httpx.Request) -> httpx.Response:
    with open(os.environ["BOUCHON_JOURNAL"], "w") as f:
        json.dump({"mask": request.headers["X-Goog-FieldMask"], "body": json.loads(request.content)}, f)
    with open(os.environ["BOUCHON_SCENARIO"]) as f:
        sc = json.load(f)
    return httpx.Response(sc["status"], json=sc["payload"])


if __name__ == "__main__":
    from rosetta.main import app

    # Après la création de l'app : la découverte des addons recharge les modules (et vide `_transport`).
    maps._transport = httpx.MockTransport(handler)
    uvicorn.run(app, port=int(sys.argv[1]), log_level="warning")
