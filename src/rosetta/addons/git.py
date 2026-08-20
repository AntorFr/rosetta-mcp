"""Git smart-HTTP proxy: the pods push real git and hold no GitHub credential.

Why this exists. The `github` addon publishes through the Git Data API, passing
file CONTENTS inline: an agent must retype every byte of every file into the tool
call. Above a few kilobytes that is a lossy channel - and a corrupted retype
rewrites the source silently. Pushing real git moves the *verified object* instead:
what was tested is what is published, bit for bit.

How it works. The pod authenticates to the hub with its ordinary rosetta JWT (the
same one every addon sees). The hub resolves the caller's GitHub App token - the
one the `github` addon already refreshes and caches - and streams the smart-HTTP
conversation to github.com. The GitHub credential never leaves the hub, so the
pod's shell cannot reach it: exactly the invariant `repo_commit` was built to
protect, kept intact while removing its cost.

Guarding is layered, each layer enforcing only what it can actually see:

  - the pod's git credential helper decides *whether to ask at all* (channel, and
    the PWA shield): it alone can know whether a human is in front;
  - this module enforces what is visible on the wire: caller identity, repository
    allowlist, and the ref rules parsed off `git-receive-pack`;
  - GitHub enforces the rest (permissions, branch protection, hook policy).

Note on force-pushes: the wire protocol carries NO force flag - a server decides
by ancestry, and GitHub allows a force-push on an unprotected branch. Relaying
verbatim would therefore protect nothing, so the ancestry check below is not
belt-and-braces: it is the only thing standing between a stray `--force` and a
rewritten history.
"""

from __future__ import annotations

import base64
import os
import re
import uuid

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from ._common import new_server
from .github import _access_token, _api, _current_sub, _slug

mcp = new_server("git")

# The GitHub App token is stored per human `sub` (shared with the `github` addon),
# so a machine identity has nothing to resolve to. Declaring it here turns that
# into a clean 403 at the door instead of a puzzling "not enrolled" further in.
identity = "user"

GITHUB_GIT = "https://github.com"
UA = "rosetta-git-proxy"

_transport = None  # crochet de test : les tests injectent un httpx.MockTransport


def _client(read: float, write: float = 30.0, redirects: bool = False) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, read=read, write=write),
        follow_redirects=redirects,
        transport=_transport,
    )

# A ref update whose new value is all zeros is a deletion.
_ZERO = "0" * 40

# Where a pack lands before its ref is promoted (see `_promote`). Under
# refs/heads/ because that is what receive-pack accepts without ceremony; the
# name is dropped again as soon as the real ref has moved.
_SCRATCH_PREFIX = "refs/heads/rosetta-scratch/"

# Commands are pkt-lines; the pack that follows them can be arbitrarily large, so
# only this prefix is ever buffered.
_MAX_COMMAND_BYTES = 64 * 1024

_SERVICES = ("git-upload-pack", "git-receive-pack")

# `<old-sha> <new-sha> <refname>` - the capabilities that may trail the first
# command after a NUL are deliberately not parsed: we never echo them back.
_COMMAND = re.compile(rb"^([0-9a-f]{40}) ([0-9a-f]{40}) ([^\x00\n]+)")


def _allowed_repos() -> set[str] | None:
    """Optional allowlist (`owner/name`, comma-separated). None = no restriction."""
    raw = os.environ.get("ROSETTA_GIT_REPOS", "").strip()
    return {_slug(r.strip()) for r in raw.split(",") if r.strip()} or None


def _refuse(message: str, status: int = 403) -> JSONResponse:
    # Git shows the body of a non-200 to the user, so this text is what lands in
    # the agent's terminal: it has to say what to do, not just that it failed.
    return JSONResponse({"error": "git_refused", "error_description": message}, status_code=status)


async def _github_headers() -> dict | JSONResponse:
    sub = _current_sub()
    if not sub:
        return _refuse("identité absente du contexte d'appel : ce proxy exige un jeton "
                       "porteur d'un sujet (machine ou humain).", status=401)
    token = await _access_token(sub)
    if isinstance(token, dict):  # {'error': ...}
        return _refuse(token["error"])
    # Basic, PAS Bearer. Le même token ouvre les deux portes de GitHub, mais pas
    # dans la même enveloppe : `api.github.com` veut un Bearer, les endpoints
    # smart-HTTP de `github.com` n'acceptent QUE Basic `x-access-token:<token>`
    # et répondent sinon un 401 « invalid credentials » — mesuré le 2026-08-10,
    # relayé tel quel au pousseur, qui le lit comme un refus du hub.
    # C'est la symétrie exacte de ce que `auth.py` fait à l'autre bout : git ne
    # sait pas envoyer un Bearer, et github.com ne sait pas en recevoir un ici.
    credential = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {"Authorization": f"Basic {credential}", "User-Agent": UA}


def _parse_commands(buffer: bytes) -> tuple[list[tuple[str, str, str]], bool]:
    """Read ref-update commands off the head of a receive-pack body.

    Returns (commands, complete). `complete` is False when the flush packet that
    closes the command list has not been seen yet - the caller must read more.
    """
    commands: list[tuple[str, str, str]] = []
    i = 0
    while i + 4 <= len(buffer):
        try:
            length = int(buffer[i:i + 4], 16)
        except ValueError:
            # Not pkt-line at all: refuse rather than guess (fail-closed).
            raise ValueError("corps git-receive-pack illisible (pkt-line attendu)")
        if length == 0:  # flush-pkt: end of the command list
            return commands, True
        if length < 4 or i + length > len(buffer):
            return commands, False
        line = buffer[i + 4:i + length]
        match = _COMMAND.match(line)
        if match:
            old, new, ref = match.groups()
            commands.append((old.decode(), new.decode(), ref.decode()))
        i += length
    return commands, False


def _check_commands(commands: list[tuple[str, str, str]]) -> str | None:
    """None if every ref update is allowed, else the reason to show the pusher.

    Purely syntactic, and deliberately so: ancestry is NOT decided here. Asking
    GitHub `/compare/<old>...<new>` before relaying could never work — `<new>` is
    precisely the commit being pushed, so GitHub answers 404 and every update of
    an existing branch was refused as "unverifiable". See `_promote`.
    """
    if not commands:
        return "aucune mise à jour de ref dans ce push."
    for old, new, ref in commands:
        if new == _ZERO:
            return (f"suppression de « {ref} » refusée : ce proxy ne supprime aucune ref. "
                    "Passer par l'interface GitHub si c'est vraiment voulu.")
        if not ref.startswith(("refs/heads/", "refs/tags/")):
            return f"ref « {ref} » hors périmètre : seuls refs/heads/* et refs/tags/* passent."
        if ref.startswith("refs/tags/") and old != _ZERO:
            # A tag is a release marker: creating one is fine, moving one rewrites
            # what a published image was built from.
            return (f"le tag « {ref} » existe déjà et ce proxy ne déplace jamais un tag. "
                    "Poser un nouveau tag plutôt que de réécrire celui-ci.")
    return None


def _pkt(payload: bytes) -> bytes:
    """Encode one pkt-line (4 hex length chars covering themselves)."""
    return b"%04x%s" % (len(payload) + 4, payload)


def _after_flush(buffer: bytes) -> int:
    """Offset just past the flush packet that closes the command list."""
    i = 0
    while i + 4 <= len(buffer):
        length = int(buffer[i:i + 4], 16)
        if length == 0:
            return i + 4
        i += length
    return len(buffer)


def _client_capabilities(buffer: bytes) -> set[bytes]:
    """What the pusher announced, after the NUL on its first command."""
    i = 0
    while i + 4 <= len(buffer):
        length = int(buffer[i:i + 4], 16)
        if length == 0:
            break
        line = buffer[i + 4:i + length]
        if b"\x00" in line:
            return set(line.split(b"\x00", 1)[1].strip().split())
        i += length
    return set()


def _report(caps: set[bytes], ref: str, reason: str | None = None):
    """A git report-status: how a push is accepted or rejected ON THE WIRE.

    An HTTP 403 is the wrong shape once the pack is flowing: git wraps the
    exchange in a side-band and the body never reaches the user, who sees only
    `RPC failed; HTTP 403` with no reason at all (measured 2026-08-10). A `ng`
    line lands in their terminal, next to the ref it refused.
    """
    if not caps & {b"report-status", b"report-status-v2"}:
        return Response(b"", media_type="application/x-git-receive-pack-result")
    status = f"ng {ref} {reason}" if reason else f"ok {ref}"
    lines = _pkt(b"unpack ok\n") + _pkt(status.encode() + b"\n") + b"0000"
    body = _pkt(b"\x01" + lines) + b"0000" if b"side-band-64k" in caps else lines
    return Response(body, media_type="application/x-git-receive-pack-result")


def _repo_of(request: Request) -> tuple[str, JSONResponse | None]:
    slug = _slug(request.path_params["repo"])
    allowed = _allowed_repos()
    if allowed is not None and slug not in allowed:
        return slug, _refuse(f"dépôt « {slug} » hors de ROSETTA_GIT_REPOS.")
    return slug, None


async def info_refs(request: Request):
    """Ref advertisement - the first half of every fetch and every push."""
    service = request.query_params.get("service", "")
    if service not in _SERVICES:
        return _refuse(f"service « {service} » inconnu.", status=400)
    slug, denied = _repo_of(request)
    if denied:
        return denied
    headers = await _github_headers()
    if isinstance(headers, JSONResponse):
        return headers

    client = _client(read=300.0, redirects=True)
    upstream = client.build_request(
        "GET", f"{GITHUB_GIT}/{slug}.git/info/refs",
        params={"service": service}, headers=headers,
    )
    try:
        response = await client.send(upstream, stream=True)
    except Exception:
        await client.aclose()
        raise

    async def body():
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(
        body(), status_code=response.status_code,
        media_type=response.headers.get("content-type", f"application/x-{service}-advertisement"),
    )


async def _proxy_pack(slug: str, service: str, rest, prefix: bytes = b""):
    """Stream one pack exchange upstream.

    `rest` is the request's byte iterator - possibly already partly consumed by
    the inspection in `receive_pack`, whose bytes come back as `prefix`. Starlette
    refuses to hand out `request.stream()` twice, so the iterator is threaded
    through rather than re-requested.
    """
    headers = await _github_headers()
    if isinstance(headers, JSONResponse):
        return headers
    headers = {
        **headers,
        "Content-Type": f"application/x-{service}-request",
        "Accept": f"application/x-{service}-result",
    }

    async def body():
        if prefix:
            yield prefix
        async for chunk in rest:
            if chunk:
                yield chunk

    # No redirect following on a streaming POST: httpx cannot replay the body, so
    # a redirect would truncate the pack. Better a loud error than a half push.
    client = _client(read=600.0, write=600.0)
    upstream = client.build_request(
        "POST", f"{GITHUB_GIT}/{slug}.git/{service}", headers=headers, content=body(),
    )
    try:
        response = await client.send(upstream, stream=True)
    except Exception:
        await client.aclose()
        raise

    async def relay():
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(
        relay(), status_code=response.status_code,
        media_type=response.headers.get("content-type", f"application/x-{service}-result"),
    )


async def _promote(slug: str, command, caps: set[bytes], rest_of_stream, buffered: bytes):
    """Land the objects on a scratch ref, then let GitHub judge the ancestry.

    This is the whole point of the detour. The wire carries no force flag, and
    `/compare` cannot answer about a commit GitHub has not received yet — but
    `PATCH /git/refs` with `force: false` refuses a non-fast-forward NATIVELY,
    on the server that owns the branch. So: push the pack to a throwaway ref
    (nothing can be overwritten there), promote through the API, drop the scratch.

    The pack is still never buffered: only the command prefix is rewritten, the
    rest of the body streams straight through.
    """
    old, new, ref = command
    headers = await _github_headers()
    if isinstance(headers, JSONResponse):
        return headers
    headers = {**headers,
               "Content-Type": "application/x-git-receive-pack-request",
               "Accept": "application/x-git-receive-pack-result"}

    scratch = f"{_SCRATCH_PREFIX}{uuid.uuid4().hex}"
    # `old` becomes zero: a ref that does not exist yet cannot be a force-push.
    prefix = _pkt(f"{_ZERO} {new} {scratch}\x00report-status".encode()) + b"0000"
    tail = buffered[_after_flush(buffered):]

    async def body():
        yield prefix
        if tail:
            yield tail
        async for chunk in rest_of_stream:
            yield chunk

    async with _client(read=300.0) as http:
        landed = await http.post(f"{GITHUB_GIT}/{slug}.git/git-receive-pack",
                                 headers=headers, content=body())
    if landed.status_code != 200 or b"unpack ok" not in landed.content:
        return _report(caps, ref, f"GitHub a refusé le pack (HTTP {landed.status_code}).")

    try:
        promoted = await _api("PATCH", f"/repos/{slug}/git/refs/{ref[len('refs/'):]}",
                              json={"sha": new, "force": False})
        if "error" in promoted:
            detail = promoted["error"]
            if "fast forward" in detail.lower():
                detail = ("mise à jour non fast-forward : ce proxy ne force jamais. "
                          "Rebaser sur origin, puis repousser.")
            return _report(caps, ref, detail)
        return _report(caps, ref)
    finally:
        # Best effort: a leftover scratch ref is noise, never a hazard.
        await _api("DELETE", f"/repos/{slug}/git/refs/{scratch[len('refs/'):]}")


async def receive_pack(request: Request):
    """A push. The commands are inspected before a single byte reaches GitHub."""
    if request.headers.get("content-encoding"):
        # git does not compress a receive-pack body (the pack is already deflated);
        # a compressed one would defeat the inspection below, so refuse it.
        return _refuse("corps compressé sur git-receive-pack : inattendu, refusé "
                       "(l'inspection des refs deviendrait aveugle).")

    slug, denied = _repo_of(request)
    if denied:
        return denied

    # Buffer only up to the flush packet closing the command list, then hand the
    # untouched prefix to the proxy: the pack itself is never held in memory.
    buffered, commands, complete = b"", [], False
    stream = request.stream()
    async for chunk in stream:
        buffered += chunk
        try:
            commands, complete = _parse_commands(buffered)
        except ValueError as exc:
            return _refuse(str(exc), status=400)
        if complete or len(buffered) > _MAX_COMMAND_BYTES:
            break
    if not complete:
        return _refuse("liste de commandes git-receive-pack tronquée ou trop longue.", status=400)

    if not commands:
        return _refuse("aucune mise à jour de ref dans ce push.")

    caps = _client_capabilities(buffered)
    reason = _check_commands(commands)
    if reason:
        return _report(caps, commands[0][2], reason)

    updates = [c for c in commands if c[0] != _ZERO]
    if not updates:
        # Creations only (new branch, new tag): nothing existing can be
        # overwritten, so there is no ancestry to judge — stream it through.
        return await _proxy_pack(slug, "git-receive-pack", stream, prefix=buffered)
    if len(commands) > 1:
        return _report(caps, updates[0][2],
                       "ce push met à jour plusieurs refs à la fois ; ce proxy en promeut "
                       "une par requête. Pousser les refs séparément.")
    return await _promote(slug, commands[0], caps, stream, buffered)


async def upload_pack(request: Request):
    """A fetch or clone. Read-only: nothing to inspect, everything to stream."""
    slug, denied = _repo_of(request)
    if denied:
        return denied
    return await _proxy_pack(slug, "git-upload-pack", request.stream())


# `:path` and not `{repo}`: a slug carries a slash (`owner/name`), which the
# default converter refuses to match.
extra_routes = [
    ("/{repo:path}/info/refs", info_refs, ["GET"]),
    ("/{repo:path}/git-receive-pack", receive_pack, ["POST"]),
    ("/{repo:path}/git-upload-pack", upload_pack, ["POST"]),
]


@mcp.tool()
async def git_remote(repo: str) -> dict:
    """L'URL de remote à donner à git pour pousser ce dépôt via le hub.

    Le pod ne détient aucun credential GitHub : c'est le hub qui authentifie, et le
    helper `git-credential-rosetta` (côté pod) qui décide si la demande part.
    """
    base = os.environ.get("ROSETTA_EXTERNAL_URL", "").rstrip("/")
    slug = _slug(repo)
    allowed = _allowed_repos()
    if allowed is not None and slug not in allowed:
        return {"error": f"dépôt « {slug} » hors de ROSETTA_GIT_REPOS."}
    return {
        "depot": slug,
        "remote": f"{base}/git/{slug}",
        "rappel": ("les refs sont bornées : pas de suppression, pas de push non "
                   "fast-forward, pas de tag déplacé."),
    }
