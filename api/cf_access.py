"""
Cloudflare Access JWT verification.

PISR's proxy mode trusts an identity header from a peer in
PISR_TRUSTED_PROXY_IPS. That is sound when the peer address means something.
Under rootless podman it does not: publishing a port rewrites the source, so
every caller arrives from inside podman's own network and the check cannot tell
the proxy from anything else that reached the port. What actually keeps a
stranger out there is the network the port is published on — one control, doing
all the work.

A verified JWT replaces that with proof. Cloudflare signs an assertion for each
authenticated request, naming the identity and the application it was minted
for; checking the signature establishes the caller came through *this* Access
application, whatever address the packet claims to be from. The network
boundary stays as defence, but it stops being the only thing there.

  Enabled by setting BOTH PISR_ACCESS_TEAM and PISR_ACCESS_AUD. Unset, proxy
  mode behaves exactly as before, so an oauth2-proxy deployment is unaffected
  and this file is never imported into the request path.

WHAT IS CHECKED, and why each one matters:

  signature  against the JWKS at <team>.cloudflareaccess.com, keyed by the
             token's `kid`. Without it the token is a text file.
  aud        must contain this application's Audience Tag. THIS IS THE ONE
             PEOPLE OMIT. Every Access application in an account is signed by
             the same keys, so a token minted for some other app — one with a
             far looser policy, or none — verifies perfectly and would be
             accepted. `aud` is what binds the token to this application.
  iss        the team's own issuer, so a token from a different Cloudflare
             account is refused.
  exp/nbf    ordinary expiry, with a little leeway for clock drift.

Identity comes from `email`, or from `common_name` when the caller is a service
token — those carry no email, and treating their absence as anonymous would let
a service token through as an unnamed user.
"""

import logging
import threading
import time
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

HEADER = "Cf-Access-Jwt-Assertion"
COOKIE = "CF_Authorization"

# How long a fetched key set is reused. Cloudflare rotates keys, and an unknown
# `kid` forces a refresh regardless, so this only bounds how long a *withdrawn*
# key stays usable.
_JWKS_TTL_SECONDS = 600

# Short on purpose. This sits in the request path, and a slow JWKS endpoint
# must fail the request rather than hold a worker open.
_JWKS_TIMEOUT_SECONDS = 5

_LEEWAY_SECONDS = 10


class AccessVerifier:
    """
    Verifies Cloudflare Access assertions. One instance, built at import in
    auth.py when configured; the key cache is shared and lock-guarded because
    reports fan out across threads.
    """

    def __init__(self, team: str, audience: str):
        self.team = team
        self.audience = audience
        self.issuer = f"https://{team}.cloudflareaccess.com"
        self.certs_url = f"{self.issuer}/cdn-cgi/access/certs"
        self._keys: Dict[str, Any] = {}
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    # ── key material ─────────────────────────────────────────────────

    def _fetch_keys(self) -> Dict[str, Any]:
        """
        Pull the JWKS and index it by kid. Raises on failure; the caller turns
        that into a refusal, never into an acceptance.
        """
        resp = requests.get(self.certs_url, timeout=_JWKS_TIMEOUT_SECONDS)
        resp.raise_for_status()
        from jwt import PyJWK  # imported here so an unconfigured deploy need not have it

        keys = {}
        for entry in (resp.json() or {}).get("keys") or []:
            kid = entry.get("kid")
            if kid:
                keys[kid] = PyJWK.from_dict(entry).key
        if not keys:
            raise ValueError(f"{self.certs_url} returned no usable keys")
        return keys

    def _key_for(self, kid: str):
        """
        The signing key for `kid`, refreshing the cache when it is stale or
        when the kid is one we have not seen — which is what a key rotation
        looks like from here.
        """
        with self._lock:
            fresh = (time.time() - self._fetched_at) < _JWKS_TTL_SECONDS
            if fresh and kid in self._keys:
                return self._keys[kid]

        # Fetched outside the lock: a slow or hanging request should not block
        # every other thread's verification behind it.
        keys = self._fetch_keys()
        with self._lock:
            self._keys = keys
            self._fetched_at = time.time()
        if kid not in keys:
            raise ValueError(f"signing key {kid!r} is not in {self.certs_url}")
        return keys[kid]

    # ── verification ─────────────────────────────────────────────────

    def verify(self, token: str) -> Optional[str]:
        """
        The identity this token proves, or None.

        None for every failure — malformed, unsigned, expired, wrong audience,
        wrong issuer, unreachable JWKS. Never raises into the request path, and
        never returns a partially-checked identity: there is no path here that
        yields a name without a verified signature behind it.
        """
        if not token:
            return None
        try:
            import jwt as pyjwt

            header = pyjwt.get_unverified_header(token)
            kid = header.get("kid")
            if not kid:
                logger.warning("Access assertion carries no kid; refusing.")
                return None

            claims = pyjwt.decode(
                token,
                key=self._key_for(kid),
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.issuer,
                leeway=_LEEWAY_SECONDS,
                options={"require": ["exp", "iat", "aud", "iss"]},
            )
        except Exception as exc:
            # The token itself is never logged: it is a bearer credential for
            # the length of its validity, and a log is a lower bar than a
            # cookie jar. The reason is enough to act on.
            logger.warning("Rejected Cloudflare Access assertion: %s: %s",
                           type(exc).__name__, exc)
            return None

        # A service token proves an application, not a person, and says so by
        # carrying common_name instead of email. Both are identities worth
        # naming in the audit line; neither is anonymous.
        identity = claims.get("email") or claims.get("common_name")
        if not identity:
            logger.warning(
                "Access assertion verified but names nobody — no email and no "
                "common_name. Refusing rather than admitting an unnamed caller.")
            return None
        return identity
