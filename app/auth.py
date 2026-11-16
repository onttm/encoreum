"""
auth.py -- identity and tiers, taken from Authentik's forward-auth headers.

WHY THERE IS NO PLEX SSO CODE HERE

    The audience for the concert queue is Plex users, and the recordings land in
    Plex — so signing in with Plex is obviously right. It does NOT follow that
    this app should implement Plex SSO.

    Authentik already brokers it. Measured 2026-08-21 on the live instance: the
    "Sign in with Plex" source has 18 linked identities, and the `friends` group
    has 18 members — the SAME 18, with zero on either side. `friends` IS the
    Plex user list. People log in with Plex; Authentik turns that into an
    identity; Traefik's `chain-authentik` passes it down as headers.

    So this app holds no sessions, no cookies and no Plex tokens. It reads five
    headers. That also means one identity system rather than two, and a Plex
    token that never exists here to be leaked.

HEADERS (set by middlewares-authentik.yml -> authResponseHeaders)
    X-authentik-username   stable login name          <- the identity we store
    X-authentik-name       display name               <- what the UI shows
    X-authentik-email
    X-authentik-uid
    X-authentik-groups     separator-delimited group list

TIERS (from actual group membership, not aspiration)
    homelab-admin    6 members  -> full operator UI, approve/decline, add sources
    friends         18 members  -> browse + request (request goes to PENDING)
    homelab-trusted  0 members  -> auto-approved within quota (defined, unused)
    homelab-media    0 members  -> browse + request
    homelab-guest    0 members  -> browse only

    Today that is really admin-vs-friends; the empty groups are wired so the
    ladder exists when you want it.

!! REQUESTERS MUST NEVER REACH THE OPERATOR UI !!
    It carries the Advanced yt-dlp passthrough (a deliberate system-hook
    surface), job cancellation, and direct library writes.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# --- header names -----------------------------------------------------------
H_USER = "x-authentik-username"
H_NAME = "x-authentik-name"
H_MAIL = "x-authentik-email"
H_UID = "x-authentik-uid"
H_GROUPS = "x-authentik-groups"

# --- roles, most privileged first ------------------------------------------
ROLE_ADMIN = "admin"          # operate, approve, manage sources
ROLE_AUTO = "auto"            # request, auto-approved within quota
ROLE_REQUESTER = "requester"  # request, needs approval
ROLE_VIEWER = "viewer"        # browse only
ROLE_NONE = "none"

GROUP_ROLES = [
    ("homelab-admin", ROLE_ADMIN),
    ("media-admin", ROLE_ADMIN),
    ("authentik Admins", ROLE_ADMIN),
    ("homelab-trusted", ROLE_AUTO),
    ("friends", ROLE_REQUESTER),
    ("homelab-media", ROLE_REQUESTER),
    ("homelab-guest", ROLE_VIEWER),
]
_RANK = {ROLE_ADMIN: 4, ROLE_AUTO: 3, ROLE_REQUESTER: 2, ROLE_VIEWER: 1, ROLE_NONE: 0}

# Authentik joins groups with "|" by default, but the separator is configurable
# and has changed across versions — split on any of the plausible ones rather
# than betting on one.
_SPLIT = re.compile(r"[|,;]")

# When false (the default, and how the app runs today) a request arriving with
# NO Authentik headers is treated as admin. That is correct for the current
# LAN-only deployment, where there is no auth at all and the operator UI is
# already wide open on the LAN.
#
# !! SET ENCOREUM_REQUIRE_AUTH=1 BEFORE PUTTING THIS BEHIND TRAEFIK !!
# Without it, anyone who reaches the container directly — bypassing the
# forward-auth middleware — is an admin.
REQUIRE_AUTH = (os.environ.get("ENCOREUM_REQUIRE_AUTH") or os.environ.get("ORPHEUM_REQUIRE_AUTH") or "").strip().lower() in (
    "1", "true", "yes", "on")


@dataclass
class Identity:
    username: str = ""
    display: str = ""
    email: str = ""
    uid: str = ""
    groups: list[str] = field(default_factory=list)
    role: str = ROLE_NONE
    authenticated: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def can_request(self) -> bool:
        return _RANK[self.role] >= _RANK[ROLE_REQUESTER]

    @property
    def auto_approved(self) -> bool:
        return _RANK[self.role] >= _RANK[ROLE_AUTO]

    @property
    def can_browse(self) -> bool:
        return _RANK[self.role] >= _RANK[ROLE_VIEWER]


def role_for(groups: list[str]) -> str:
    """Highest role any of the user's groups grants."""
    best = ROLE_NONE
    for g in groups:
        for name, role in GROUP_ROLES:
            if g.strip().lower() == name.lower() and _RANK[role] > _RANK[best]:
                best = role
    return best


def identify(request) -> Identity:
    """Build an Identity from forward-auth headers on a Starlette/FastAPI request."""
    h = request.headers
    user = (h.get(H_USER) or "").strip()

    if not user:
        if REQUIRE_AUTH:
            # Fail closed: no headers means the request did not come through
            # the forward-auth middleware.
            return Identity(role=ROLE_NONE, authenticated=False)
        # LAN/dev: behave exactly as the app did before auth existed.
        return Identity(username="local", display="local (unauthenticated)",
                        role=ROLE_ADMIN, authenticated=False)

    groups = [g for g in _SPLIT.split(h.get(H_GROUPS) or "") if g.strip()]
    return Identity(
        username=user,
        display=(h.get(H_NAME) or user).strip(),
        email=(h.get(H_MAIL) or "").strip(),
        uid=(h.get(H_UID) or "").strip(),
        groups=groups,
        role=role_for(groups),
        authenticated=True,
    )
