"""
The identity trace — one DPSK username followed to the APs it can land on.

    username ──► policy set ──► policy (by its username regex, in priority order)
                                  │
                                  └─ SSID regex ──► network ──► activated here?
                                                                  │
                                                         AP groups ──► APs

The report's policy card says a set exists and how many conditions each policy
has. This says whether they are the RIGHT conditions: the typical MDU build is
one policy per resident, each with two regex conditions — `^username$` and
`^SSID$` — and the faults it exists to catch are the ones a count cannot see. A
policy named for a unit whose username regex has a typo; an SSID regex naming a
network that was renamed; a network that exists but is not activated here; an
AP group with nothing in it.

PURE. Everything R1 said arrives as arguments, from the route in pisr_router,
and nothing here fetches. The regexes are evaluated with Python's `re`; R1's
are Java's, which differ only at the edges (possessive and lookbehind corners),
and the patterns seen live are anchored literals where the two agree exactly. A
pattern `re` cannot compile is reported as unverifiable, never as a mismatch.

NOT PART OF THE REPORT, and that is a boundary rather than an accident. The
rows name residents' DPSK usernames, which `shape._dpsk_safe` refuses to put
in a report on purpose. The route serves this to admins only, on a button
press, and it is never persisted — the same as every other PISR response.
"""

import re
from functools import lru_cache

from services.pisr.shape import _rate_limits
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Condition attributes, by R1's stable machine name (`attributeTextMatch`).
# The ids are template-specific (1012 in DPSK, none in RADIUS), and the display
# name is prose with quotes in it, so neither is matched on.
USERNAME_MATCH = "dpsk_username"
SSID_MATCH = "ssid"

# Worst first. A row's status is the worst of its issues. A NOTE is shown with
# the row but never makes it non-OK: it describes a legitimate design that is
# not the per-unit one (a tier policy with no SSID condition, for one).
SEVERITY = {"error": 0, "warning": 1, "unknown": 2, "ok": 3, "note": 4}

ISSUES: Dict[str, Tuple[str, str]] = {
    "no-set": ("error", "No policy set on this user's DPSK pool or identity group"),
    # Split on the pool's `policyDefaultAccess`, because the same miss means
    # opposite things: on an accept-by-default pool (351 of 357 live) the user
    # is online with no tier — normal for the base tier of a tiered design, a
    # fault in a per-unit one; on a reject-by-default pool they cannot join.
    "rejected": ("error", "Matches no policy, and the pool rejects by default"),
    "default-access": ("warning", "Matches no policy — falls through to the pool's default access"),
    "regex-typo": ("error", "A policy is named for this user but its username regex does not match"),
    "ssid-no-network": ("error", "SSID condition matches no network on the tenant"),
    "ssid-not-here": ("error", "Matched network is not activated on this venue"),
    "ssid-not-pool": ("warning", "Matched network is not backed by this user's DPSK pool"),
    "shadowed": ("warning", "More than one policy matches — the lower-priority ones never apply"),
    "no-ssid-condition": ("note", "Policy has no SSID condition — it applies on every SSID"),
    "no-aps": ("warning", "Network is activated only on AP groups with no APs"),
    "conditions-unread": ("unknown", "Policy conditions could not be read"),
    "regex-invalid": ("unknown", "A regex could not be evaluated here"),
}


@lru_cache(maxsize=8192)
def _compile(pattern: str):
    try:
        return re.compile(pattern)
    except re.error:
        return None


def _matches(pattern: Optional[str], value: Optional[str]) -> Optional[bool]:
    """True/False, or None when the pattern cannot be evaluated here."""
    if pattern is None or value is None:
        return None
    compiled = _compile(pattern)
    if compiled is None:
        return None
    return compiled.search(value) is not None


def _all_match(patterns: List[str], value: Optional[str]) -> Optional[bool]:
    """Conditions of one kind AND together; any unevaluable one makes it None."""
    results = [_matches(p, value) for p in patterns]
    if any(r is False for r in results):
        return False
    if any(r is None for r in results):
        return None
    return True


def _split_conditions(conditions: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    out = {"username": [], "ssid": [], "other": []}
    for c in conditions or []:
        match = str(c.get("match") or "").lower()
        if match == USERNAME_MATCH:
            out["username"].append(c.get("regex") or "")
        elif match == SSID_MATCH:
            out["ssid"].append(c.get("regex") or "")
        else:
            out["other"].append({"attribute": c.get("attribute") or c.get("match"),
                                 "regex": c.get("regex")})
    return out


def _worst(issues: Iterable[str]) -> str:
    worst = "ok"
    for code in issues:
        sev = ISSUES[code][0]
        if SEVERITY[sev] < SEVERITY[worst]:
            worst = sev
    return worst


def _resolve_vlans(row: Dict[str, Any], entry: Dict[str, Any],
                   network_out: Dict[str, Dict[str, Any]], default_vlans,
                   fallback: Tuple[List[str], Optional[str]] = ([], None)) -> None:
    """
    The VLAN this passphrase's clients get, and its devices counted by VLAN.

    R1's order, as the operator describes it: the PASSPHRASE's VLAN if set,
    else the IDENTITY's, else the network's default at this venue. The first
    two apply whatever network a device joined; only the third depends on it,
    so devices are grouped per last-connected network only in that case.

    `row["vlan"]` is {"vlans": [...], "source": "passphrase"|"identity"|
    "network"|None, "basis": ...}. The network leg is THIS USER'S network,
    never the pool's whole list — on a per-unit venue the pool backs hundreds
    of SSIDs, each with its own VLAN. `basis` says how it was identified:
    "policy" (the SSID regex of the policy that selected them), or, for a user
    no policy selected, whatever `fallback` found — see their_networks in
    identity_trace.
    """
    fixed, source = None, None
    if row["passphraseVlan"] is not None:
        fixed, source = row["passphraseVlan"], "passphrase"
    elif row["identityVlan"] is not None:
        fixed, source = row["identityVlan"], "identity"

    if fixed is not None:
        row["vlan"] = {"vlans": [fixed], "source": source, "basis": None}
    elif row["networkIds"]:
        here = [nid for nid in row["networkIds"] if network_out.get(nid, {}).get("activated")]
        vlans = sorted({v for nid in here for v in network_out[nid]["defaultVlans"]})
        row["vlan"] = {"vlans": vlans, "source": "network" if vlans else None,
                       "basis": "policy" if vlans else None}
    else:
        nids, basis = fallback
        vlans = sorted({v for nid in nids for v in default_vlans(nid)})
        row["vlan"] = {"vlans": vlans, "source": "network" if vlans else None,
                       "basis": basis if vlans else None}

    buckets: Dict[Tuple, int] = {}
    for nid, count in (entry.get("devicesByNetwork") or {}).items():
        if fixed is not None:
            key = ((fixed,), source)
        else:
            vlans = tuple(default_vlans(nid)) if nid else ()
            key = (vlans, "network" if vlans else None)
        buckets[key] = buckets.get(key, 0) + count
    row["devicesByVlan"] = sorted(
        ({"vlans": list(vlans), "source": src, "count": n}
         for (vlans, src), n in buckets.items()),
        key=lambda b: -b["count"])


def identity_trace(*,
                   pool_rows: List[Dict[str, Any]],
                   raw_pools: List[Dict[str, Any]],
                   usernames: Dict[str, Dict[str, Any]],
                   identities: Optional[Dict[str, Dict[str, Any]]] = None,
                   radius_groups: Optional[List[Dict[str, Any]]] = None,
                   sets: List[Dict[str, Any]],
                   scoped_set_ids: set,
                   set_members: Dict[str, List[Dict[str, Any]]],
                   policies: List[Dict[str, Any]],
                   conditions: Dict[str, Optional[List[Dict[str, Any]]]],
                   conditions_wanted: int,
                   networks: List[Dict[str, Any]],
                   activations: List[Dict[str, Any]],
                   aps: List[Dict[str, Any]],
                   ap_groups: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Every scoped DPSK username, traced. NORMALISED: policies, networks, AP
    groups and APs are sent once as maps and rows name them by id, because a
    per-unit MDU has a thousand rows and repeating each AP list per row would
    multiply the payload by the fleet size.
    """
    policy_by_id = {p.get("id"): p for p in policies}
    set_by_id = {s.get("id"): s for s in sets}
    raw_pool_by_id = {p.get("id"): p for p in raw_pools}
    network_by_id = {n.get("id"): n for n in networks}
    radius_by_id = {g.get("id"): g for g in radius_groups or []}

    # Identity name and description, joined to a passphrase row by its
    # `identityId`, or by the identity's `dpskGuid` (the passphrase id) when a
    # row does not carry one.
    identity_by_id: Dict[str, Dict[str, Any]] = {}
    identity_by_guid: Dict[str, Dict[str, Any]] = {}
    for result in (identities or {}).values():
        for ident in result.get("rows") or []:
            if ident.get("id"):
                identity_by_id[ident["id"]] = ident
            if ident.get("dpskGuid"):
                identity_by_guid[ident["dpskGuid"]] = ident

    # ── networks: every tenant network, since an SSID regex naming a network
    # that exists but is not activated here is exactly one of the faults.
    activation_by_net = {a.get("networkId"): a for a in activations if a.get("networkId")}
    all_group_ids = [g.get("id") for g in ap_groups if g.get("id")]
    group_names = {g.get("id"): g.get("name") for g in ap_groups}
    aps_by_group: Dict[str, List[str]] = {}
    for ap in aps:
        if ap.get("serial"):
            aps_by_group.setdefault(ap.get("apGroupId"), []).append(ap["serial"])

    def activation_groups(activation: Dict[str, Any]) -> Tuple[bool, List[str]]:
        # An isAllApGroups activation also ENUMERATES every group as a formality
        # (see shape.wireless_card) — it is one scope, all of them.
        if activation.get("isAllApGroups"):
            return True, list(all_group_ids)
        return False, [g.get("apGroupId") for g in activation.get("apGroups") or []
                       if g.get("apGroupId")]

    def default_vlans(nid: str) -> List[int]:
        """
        The VLAN a client on this network gets when neither its passphrase
        nor its identity names one: the venue activation's override if it has
        one (venue-wide, or per AP group — which can differ, hence a list),
        else the network's own VLAN.
        """
        activation = activation_by_net.get(nid) or {}
        vlans = set()
        if activation.get("allApGroupsVlanId") is not None:
            vlans.add(activation["allApGroupsVlanId"])
        elif not activation.get("isAllApGroups"):
            vlans.update(g.get("vlanId") for g in activation.get("apGroups") or []
                         if g.get("vlanId") is not None)
        if not vlans and (network_by_id.get(nid) or {}).get("vlan") is not None:
            vlans.add(network_by_id[nid]["vlan"])
        return sorted(int(v) for v in vlans if str(v).lstrip("-").isdigit())

    # How many passphrases each identity holds, across every pool read. One
    # each on every tenant probed, but the model allows several.
    passphrases_per_identity: Dict[str, int] = {}
    for result in usernames.values():
        for entry in result.get("rows") or []:
            if entry.get("identityId"):
                passphrases_per_identity[entry["identityId"]] = \
                    passphrases_per_identity.get(entry["identityId"], 0) + 1

    # ── policies in scope, with their conditions split by kind.
    policy_out: Dict[str, Dict[str, Any]] = {}
    set_policies: Dict[str, List[str]] = {}
    for sid in sorted(scoped_set_ids):
        members = sorted(set_members.get(sid) or [],
                         key=lambda m: (m.get("priority") is None, m.get("priority") or 0))
        ids = []
        for m in members:
            pid = m.get("policyId")
            policy = policy_by_id.get(pid)
            if not policy:
                continue  # an unresolved member is the report's policy-chain finding
            ids.append(pid)
            split = _split_conditions(conditions.get(pid))
            radius_id = policy.get("onMatchResponse")
            radius = radius_by_id.get(radius_id)
            policy_out[pid] = {
                "name": policy.get("name"),
                "setId": sid,
                "priority": m.get("priority"),
                "conditionsCount": policy.get("conditionsCount"),
                "conditionsRead": conditions.get(pid) is not None,
                "usernameRegex": split["username"],
                "ssidRegex": split["ssid"],
                "otherConditions": split["other"],
                # The RADIUS attribute group the policy hands back on match —
                # the rate tier this user actually gets.
                "radiusGroup": (radius or {}).get("name"),
                "radiusGroupMissing": bool(radius_id) and radius is None,
                "rateLimits": _rate_limits(radius) if radius else [],
                # A RADIUS group can hand back a VLAN itself. Surfaced, not
                # folded into the resolution: whether R1 lets it beat a
                # passphrase VLAN has not been verified.
                "radiusVlan": next((a.get("attributeValue") for a in
                                    (radius or {}).get("attributeAssignments") or []
                                    if "tunnel-private-group" in str(a.get("attributeName")).lower()),
                                   None),
                "claimedBy": 0,
            }
        set_policies[sid] = ids

    network_out: Dict[str, Dict[str, Any]] = {}
    referenced_groups: set = set()

    def note_network(nid: str) -> None:
        if nid in network_out:
            return
        net = network_by_id.get(nid) or {}
        activation = activation_by_net.get(nid)
        all_groups, gids = activation_groups(activation) if activation else (False, [])
        referenced_groups.update(gids)
        network_out[nid] = {
            "ssid": net.get("ssid") or net.get("name") or nid,
            "name": net.get("name"),
            "activated": activation is not None,
            "defaultVlans": default_vlans(nid),
            "allApGroups": all_groups,
            "apGroupIds": gids,
        }

    # SSID regex -> networks, memoised per pattern set: a thousand policies
    # sharing a handful of SSID patterns should not scan the tenant a thousand
    # times.
    ssid_cache: Dict[Tuple[str, ...], Tuple[List[str], bool]] = {}

    def networks_for(patterns: List[str]) -> Tuple[List[str], bool]:
        key = tuple(patterns)
        if key not in ssid_cache:
            hits, unevaluable = [], False
            for n in networks:
                result = _all_match(patterns, n.get("ssid") or n.get("name"))
                if result is None:
                    unevaluable = True
                elif result:
                    hits.append(n.get("id"))
            ssid_cache[key] = (hits, unevaluable)
        return ssid_cache[key]

    def their_networks(row: Dict[str, Any], entry: Dict[str, Any],
                       pool_nets: set) -> Tuple[List[str], Optional[str]]:
        """
        THIS user's network(s) here when no policy selected them — so a network
        default can be named without pretending they are on all of the pool's.

        Evidence, strongest first: the networks their devices actually last
        connected on; the SSID regex of the policy NAMED for them (the typo
        case — it says which network they were meant to have); the pool's only
        network here, when it backs exactly one (a shared-SSID tier build).
        Otherwise nothing, and the VLAN reads "could not be determined".
        """
        used = sorted(nid for nid in (entry.get("devicesByNetwork") or {})
                      if nid in activation_by_net)
        if used:
            return used, "devices"
        named = policy_out.get(row.get("namedPolicyId") or "")
        if named and named["ssidRegex"]:
            hits = [nid for nid in networks_for(named["ssidRegex"])[0]
                    if nid in activation_by_net]
            if hits:
                return hits, "named-policy"
        only = sorted(pool_nets & set(activation_by_net))
        if len(only) == 1:
            return only, "only-network"
        return [], None

    # ── rows
    rows = []
    limits_users = {"shown": 0, "total": 0}
    for pool in pool_rows:
        pool_id = pool.get("id")
        pool_nets = set((raw_pool_by_id.get(pool_id) or {}).get("networkIds") or [])
        group_sets = {g.get("id"): g.get("policySetId") for g in pool.get("identityGroups") or []}
        identity_group_names = {g.get("id"): g.get("name") for g in pool.get("identityGroups") or []}
        result = usernames.get(pool_id) or {"rows": [], "total": 0}
        limits_users["shown"] += len(result.get("rows") or [])
        limits_users["total"] += int(result.get("total") or 0)

        for entry in result.get("rows") or []:
            username = entry.get("username") or ""
            issues: List[str] = []
            set_id = pool.get("policySetId") or group_sets.get(entry.get("identityGroupId"))
            ident = (identity_by_id.get(entry.get("identityId"))
                     or identity_by_guid.get(entry.get("id")) or {})
            gid = entry.get("identityGroupId")
            row: Dict[str, Any] = {
                "username": username,
                "pool": pool.get("name"),
                "poolId": pool_id,
                "identityGroupId": gid,
                "identityGroup": identity_group_names.get(gid),
                "identityName": ident.get("name"),
                # None, not "", when unset: the table's tick keys off presence.
                "description": (ident.get("description") or "").strip() or None,
                "identityId": entry.get("identityId"),
                "passphraseCount": passphrases_per_identity.get(entry.get("identityId"), 1),
                "passphraseVlan": entry.get("vlanId"),
                "identityVlan": ident.get("vlan"),
                "deviceCount": int(entry.get("deviceCount") or 0),
                "devicesByVlan": [],
                "vlan": None,
                "setId": set_id,
                "policyId": None,
                "shadowedBy": [],
                "shadows": [],
                "namedPolicyId": None,
                "networkIds": [],
            }
            if not set_id:
                issues.append("no-set")
                _resolve_vlans(row, entry, network_out, default_vlans,
                               their_networks(row, entry, pool_nets))
                row.update(issues=issues, status=_worst(issues))
                rows.append(row)
                continue

            candidates, unread, unevaluable = [], False, False
            for pid in set_policies.get(set_id, []):
                p = policy_out[pid]
                if str(p["name"] or "").casefold() == username.casefold():
                    row["namedPolicyId"] = pid
                if not p["conditionsRead"]:
                    unread = True
                    continue
                if not p["usernameRegex"]:
                    # No username condition: it matches every user, subject to
                    # its other conditions. Still a candidate — in priority
                    # order it will catch this user if nothing earlier does.
                    candidates.append(pid)
                    continue
                verdict = _all_match(p["usernameRegex"], username)
                if verdict is None:
                    unevaluable = True
                    issues.append("regex-invalid")
                elif verdict:
                    candidates.append(pid)

            if candidates:
                winner = candidates[0]
                row["policyId"] = winner
                policy_out[winner]["claimedBy"] += 1
                # Only SPECIFIC matches count as shadowing. A catch-all policy
                # after the winner is a fallback, not a conflict.
                specific = [pid for pid in candidates[1:] if policy_out[pid]["usernameRegex"]]
                if specific:
                    row["shadows"] = specific
                    issues.append("shadowed")
            elif unread:
                issues.append("conditions-unread")
            elif unevaluable:
                # The regex we could not evaluate may well be the one that
                # matches. "Falls through" would be a verdict we cannot back.
                pass
            else:
                issues.append("default-access" if pool.get("policyDefaultAccess") is not False
                              else "rejected")

            named = row["namedPolicyId"]
            if named and named != row["policyId"] and policy_out[named]["conditionsRead"] \
                    and _all_match(policy_out[named]["usernameRegex"], username) is False:
                issues.append("regex-typo")

            winner = row["policyId"]
            if winner:
                p = policy_out[winner]
                if p["ssidRegex"]:
                    hits, unevaluable = networks_for(p["ssidRegex"])
                    if unevaluable and not hits:
                        issues.append("regex-invalid")
                    elif not hits:
                        issues.append("ssid-no-network")
                    row["networkIds"] = hits
                else:
                    issues.append("no-ssid-condition")
                    # Applies on any SSID; the ones that matter are this pool's.
                    row["networkIds"] = sorted(pool_nets & set(activation_by_net))
                for nid in row["networkIds"]:
                    note_network(nid)
                here = [nid for nid in row["networkIds"] if network_out[nid]["activated"]]
                if row["networkIds"] and not here:
                    issues.append("ssid-not-here")
                if here and pool_nets and not (set(here) & pool_nets):
                    issues.append("ssid-not-pool")
                if here and not any(aps_by_group.get(g)
                                    for nid in here for g in network_out[nid]["apGroupIds"]):
                    issues.append("no-aps")

            # A user a reject-by-default pool turns away gets no VLAN at all,
            # so there is no network default to fall back to.
            _resolve_vlans(row, entry, network_out, default_vlans,
                           ([], None) if "rejected" in issues
                           else their_networks(row, entry, pool_nets))

            # De-dupe while keeping order; a regex-invalid can fire twice.
            issues = list(dict.fromkeys(issues))
            row.update(issues=issues, status=_worst(issues))
            rows.append(row)

    rows.sort(key=lambda r: (SEVERITY[r["status"]], r["username"].casefold()))

    group_out = {
        gid: {"name": group_names.get(gid) or gid,
              "apSerials": sorted(aps_by_group.get(gid) or [])}
        for gid in referenced_groups
    }
    ap_out = {
        ap["serial"]: {"name": ap.get("name"), "model": ap.get("model"),
                       "state": ap.get("state"), "status": ap.get("status")}
        for ap in aps
        if ap.get("serial") and ap.get("apGroupId") in referenced_groups
    }

    by_issue: Dict[str, int] = {}
    for row in rows:
        for code in row["issues"]:
            by_issue[code] = by_issue.get(code, 0) + 1
    by_status: Dict[str, int] = {}
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + 1

    # Policies no username lands on: a resident who moved out, usually. Only
    # meaningful when every username was read and every condition was.
    unclaimed = [pid for pid, p in policy_out.items()
                 if p["claimedBy"] == 0 and p["conditionsRead"] and p["usernameRegex"]]

    return {
        "rows": rows,
        "policies": policy_out,
        "sets": [{"id": sid, "name": (set_by_id.get(sid) or {}).get("name"),
                  "policyCount": len(set_policies.get(sid, []))}
                 for sid in sorted(scoped_set_ids)],
        "networks": network_out,
        "apGroups": group_out,
        "aps": ap_out,
        "unclaimedPolicyIds": sorted(unclaimed,
                                     key=lambda pid: str(policy_out[pid]["name"] or "")),
        "summary": {"total": len(rows), "byStatus": by_status, "byIssue": by_issue},
        "issues": {code: {"severity": sev, "label": label}
                   for code, (sev, label) in ISSUES.items()},
        "limits": {
            "usernames": {**limits_users,
                          "complete": limits_users["shown"] >= limits_users["total"]},
            "conditions": {"read": sum(1 for v in conditions.values() if v is not None),
                           "wanted": conditions_wanted},
        },
    }
