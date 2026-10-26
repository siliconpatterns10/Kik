"""Attendee to user-record mapping, by exact rules only.

The four permitted methods, in the order they are attempted:

1. `EXACT_EMAIL`       - normalised (trimmed, case-folded) equality.
2. `EXPLICIT_ALIAS`    - a `speaker_aliases` row naming the label, resolved to
                         its user. Meeting-scoped rows beat global ones.
3. `UNIQUE_EXACT_NAME` - one active user whose normalised display name equals
                         the normalised label, and no other user competing.
                         Labelled name-based: it is not email-verified.
4. `UNMATCHED`, `AMBIGUOUS`, `INACTIVE` - reported, never resolved.

**No fuzzy matching.** The application's TypeScript resolver also accepts a
single-character edit in one token of a multi-token name; that rule is
deliberately not ported, because mechanical near-miss resolution in a tool whose
output drives mail is not something a task agent should inherit silently. A
near-miss is an agent's call, made from the returned evidence.

The alias uniqueness rule mirrors `src/brain/speaker-resolver.ts`
(`buildAliasIndex`): the index is keyed on the normalised raw label and a
`MEETING`-scoped row wins over a `GLOBAL` one. Mapping a colliding alias *set*
to a single "winner" required reading five interacting facts, so this module
reports the collision instead of resolving it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

EXACT_EMAIL = "EXACT_EMAIL"
EXPLICIT_ALIAS = "EXPLICIT_ALIAS"
UNIQUE_EXACT_NAME = "UNIQUE_EXACT_NAME"
UNMATCHED = "UNMATCHED"
AMBIGUOUS = "AMBIGUOUS"
INACTIVE = "INACTIVE"

RESOLVED_STATES = frozenset({EXACT_EMAIL, EXPLICIT_ALIAS, UNIQUE_EXACT_NAME})

#: Fields allowed out of `users`. Deliberately excludes everything about
#: credentials, invitations and identity-provider linkage: this toolkit needs to
#: know who someone is and whether they are active, and nothing else.
USER_COLUMNS: tuple[str, ...] = ("id", "email", "displayName", "isActive", "roles", "dept")

#: Fields allowed out of `speaker_aliases`.
ALIAS_COLUMNS: tuple[str, ...] = (
    "id",
    "rawLabel",
    "kind",
    "userId",
    "canonicalName",
    "scope",
    "meetingId",
)


def normalize_label(label: str | None) -> str:
    """Case, whitespace and trailing punctuation only. Mirrors normalizeLabel."""
    if label is None:
        return ""
    text = str(label)
    for zero_width in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        text = text.replace(zero_width, "")
    text = " ".join(text.split()).strip()
    text = text.rstrip(".,;:")
    return text.lower()


def normalize_email(value: str | None) -> str:
    return str(value or "").strip().lower()


@dataclass
class UserRecord:
    id: str
    email: str | None
    display_name: str | None
    is_active: bool | None
    roles: list[str] = field(default_factory=list)
    dept: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "userId": self.id,
            "email": self.email,
            "displayName": self.display_name,
            "isActive": self.is_active,
            "roles": self.roles,
            "dept": self.dept,
        }


@dataclass
class AliasRecord:
    id: str
    raw_label: str
    kind: str | None
    user_id: str | None
    canonical_name: str | None
    scope: str | None
    meeting_id: str | None

    @property
    def normalized_label(self) -> str:
        return normalize_label(self.raw_label)

    def as_dict(self) -> dict[str, Any]:
        return {
            "aliasId": self.id,
            "rawLabel": self.raw_label,
            "kind": self.kind,
            "userId": self.user_id,
            "canonicalName": self.canonical_name,
            "scope": self.scope,
            "meetingId": self.meeting_id,
        }


def build_user_index(rows: Iterable[dict[str, Any]]) -> list[UserRecord]:
    users: list[UserRecord] = []
    for row in rows:
        roles = row.get("roles") or []
        users.append(
            UserRecord(
                id=str(row.get("id")),
                email=row.get("email"),
                display_name=row.get("displayName"),
                is_active=row.get("isActive"),
                roles=[str(r) for r in roles] if isinstance(roles, list) else [],
                dept=row.get("dept"),
            )
        )
    return users


def build_alias_index(rows: Iterable[dict[str, Any]]) -> dict[str, list[AliasRecord]]:
    """Group aliases by normalised raw label, keeping every row.

    The application collapses this to one row per label with a meeting-scoped
    precedence rule. Grouping instead lets `resolve_label` report a collision
    rather than hide it.
    """
    index: dict[str, list[AliasRecord]] = {}
    for row in rows:
        alias = AliasRecord(
            id=str(row.get("id")),
            raw_label=str(row.get("rawLabel") or ""),
            kind=row.get("kind"),
            user_id=row.get("userId"),
            canonical_name=row.get("canonicalName"),
            scope=row.get("scope"),
            meeting_id=row.get("meetingId"),
        )
        key = alias.normalized_label
        if not key:
            continue
        index.setdefault(key, []).append(alias)
    return index


@dataclass
class Mapping:
    label: str
    identity: str | None
    state: str
    evidence: str
    user: dict[str, Any] | None = None
    alias: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    alternatives_considered: list[dict[str, Any]] = field(default_factory=list)
    requires_agent_review: bool = False
    email_verified: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "identity": self.identity,
            "state": self.state,
            "evidence": self.evidence,
            "emailVerified": self.email_verified,
            "requiresAgentReview": self.requires_agent_review,
            "user": self.user,
            "alias": self.alias,
            "candidates": self.candidates,
            "alternativesConsidered": self.alternatives_considered,
        }


def map_email(
    email: str | None,
    *,
    label: str | None = None,
    users: list[UserRecord],
) -> Mapping:
    """Exact email match. The only email-verified rule."""
    shown_label = label or email or "(no email)"
    normalized = normalize_email(email)
    if not normalized:
        return Mapping(
            label=shown_label,
            identity=None,
            state=UNMATCHED,
            evidence="no email address supplied",
            requires_agent_review=True,
        )

    matches = [u for u in users if normalize_email(u.email) == normalized]
    if not matches:
        return Mapping(
            label=shown_label,
            identity=None,
            state=UNMATCHED,
            evidence=f"no user row has email {normalized}",
            requires_agent_review=True,
        )
    if len(matches) > 1:
        return Mapping(
            label=shown_label,
            identity=None,
            state=AMBIGUOUS,
            evidence=(
                f"{len(matches)} user rows share email {normalized}; "
                "email is unique in the schema, so this indicates a data problem"
            ),
            candidates=[u.as_dict() for u in matches],
            requires_agent_review=True,
        )

    user = matches[0]
    if user.is_active is False:
        return Mapping(
            label=shown_label,
            identity=None,
            state=INACTIVE,
            evidence=f"exact email match to an inactive user ({normalized})",
            user=user.as_dict(),
            email_verified=True,
            requires_agent_review=True,
        )
    return Mapping(
        label=shown_label,
        identity=user.display_name or user.id,
        state=EXACT_EMAIL,
        evidence=f"normalised email equality: {normalized}",
        user=user.as_dict(),
        email_verified=True,
    )


def resolve_label(
    label: str | None,
    *,
    users: list[UserRecord],
    alias_index: dict[str, list[AliasRecord]],
    meeting_id: str | None = None,
) -> Mapping:
    """Resolve a transcript speaker label or attendee name. Exact rules only."""
    raw = str(label or "")
    key = normalize_label(raw)
    if not key:
        return Mapping(
            label=raw or "(empty label)",
            identity=None,
            state=UNMATCHED,
            evidence="empty label",
            requires_agent_review=True,
        )

    rows = alias_index.get(key, [])
    if rows:
        return _from_aliases(raw, rows, users=users, meeting_id=meeting_id)

    return _from_name(raw, users=users)


def _from_aliases(
    raw: str,
    rows: list[AliasRecord],
    *,
    users: list[UserRecord],
    meeting_id: str | None,
) -> Mapping:
    """Apply the application's precedence, then report what is left.

    `buildAliasIndex` in `src/brain/speaker-resolver.ts` keeps one row per
    normalised label and lets a `MEETING`-scoped row beat a `GLOBAL` one. That
    narrowing is reproduced here, but the *collision case inside one scope* is
    reported rather than arbitrated, because the mapping decides who is mailed.
    """
    # A meeting-scoped row only applies when it belongs to the meeting in hand;
    # a row scoped to a different meeting must not outrank the global default.
    meeting_rows = [
        r
        for r in rows
        if r.scope == "MEETING" and (meeting_id is None or r.meeting_id == meeting_id)
    ]
    applicable = meeting_rows or [r for r in rows if r.scope != "MEETING"]

    if len(applicable) > 1:
        scope_label = "MEETING-scoped" if meeting_rows else "GLOBAL/unspecific"
        return Mapping(
            label=raw,
            identity=None,
            state=AMBIGUOUS,
            evidence=(
                f"{len(applicable)} {scope_label} alias rows share the normalised "
                f"label {raw.strip()!r}; this toolkit will not choose between people"
            ),
            candidates=[r.as_dict() for r in rows],
            requires_agent_review=True,
        )
    if not applicable:
        return _from_name(raw, users=users)

    alias = applicable[0]

    if alias.kind != "PERSON":
        return Mapping(
            label=raw,
            identity=None,
            state=UNMATCHED,
            evidence=(
                f"explicit alias exists but its kind is {alias.kind!r}, not PERSON; "
                "an account or unknown label resolves to nobody"
            ),
            alias=alias.as_dict(),
            requires_agent_review=True,
        )
    if not alias.user_id:
        return Mapping(
            label=raw,
            identity=None,
            state=UNMATCHED,
            evidence="explicit PERSON alias has no linked user",
            alias=alias.as_dict(),
            requires_agent_review=True,
        )

    user = next((u for u in users if u.id == alias.user_id), None)
    if user is None:
        return Mapping(
            label=raw,
            identity=None,
            state=UNMATCHED,
            evidence=(
                f"explicit alias links to user {alias.user_id}, which is not in the "
                "fetched user set"
            ),
            alias=alias.as_dict(),
            requires_agent_review=True,
        )
    if user.is_active is False:
        return Mapping(
            label=raw,
            identity=None,
            state=INACTIVE,
            evidence=(
                f"explicit alias links to inactive user {user.display_name or user.id}"
            ),
            user=user.as_dict(),
            alias=alias.as_dict(),
            requires_agent_review=True,
        )
    return Mapping(
        label=raw,
        identity=user.display_name or user.id,
        state=EXPLICIT_ALIAS,
        evidence=(
            f"speaker_aliases row {alias.id} maps {raw.strip()!r} "
            f"(scope {alias.scope}, kind {alias.kind}) to user {user.id}"
        ),
        user=user.as_dict(),
        alias=alias.as_dict(),
    )


def _from_name(raw: str, *, users: list[UserRecord]) -> Mapping:
    key = normalize_label(raw)

    active = [u for u in users if u.is_active is not False]
    inactive = [u for u in users if u.is_active is False]

    name_matches = [u for u in active if normalize_label(u.display_name) == key]
    if len(name_matches) == 1:
        user = name_matches[0]
        return Mapping(
            label=raw,
            identity=user.display_name or user.id,
            state=UNIQUE_EXACT_NAME,
            evidence=(
                f"single active user whose normalised display name equals {key!r}; "
                "name-based, not email-verified"
            ),
            user=user.as_dict(),
            email_verified=False,
        )
    if len(name_matches) > 1:
        return Mapping(
            label=raw,
            identity=None,
            state=AMBIGUOUS,
            evidence=(
                f"{len(name_matches)} active users share the normalised display name "
                f"{key!r}"
            ),
            candidates=[u.as_dict() for u in name_matches],
            requires_agent_review=True,
        )

    inactive_matches = [u for u in inactive if normalize_label(u.display_name) == key]
    if len(inactive_matches) == 1:
        user = inactive_matches[0]
        return Mapping(
            label=raw,
            identity=None,
            state=INACTIVE,
            evidence=(
                f"normalised display name matches inactive user "
                f"{user.display_name or user.id}"
            ),
            user=user.as_dict(),
            requires_agent_review=True,
        )

    # No exact match. Report near-name possibilities only as a labelled list of
    # alternatives, never as a resolution: the agent decides, with the evidence.
    alternatives = []
    tokens = set(key.split())
    for user in active:
        user_tokens = set(normalize_label(user.display_name).split())
        if not tokens or not user_tokens:
            continue
        overlap = len(tokens & user_tokens) / max(len(tokens), len(user_tokens))
        if overlap:
            alternatives.append(
                {
                    "userId": user.id,
                    "displayName": user.display_name,
                    "tokenOverlap": round(overlap, 3),
                    "note": "name-based proximity only; not a match and not a recipient",
                }
            )
    alternatives.sort(key=lambda item: (-item["tokenOverlap"], str(item["displayName"])))

    return Mapping(
        label=raw,
        identity=None,
        state=UNMATCHED,
        evidence=(
            "no exact normalised email, alias or display-name match; fuzzy matching "
            "is deliberately not implemented"
            + ("" if not inactive_matches else f"; {len(inactive_matches)} inactive name match(es)")
        ),
        candidates=alternatives[:5],
        alternatives_considered=alternatives[:5],
        requires_agent_review=True,
    )


def map_attendees(
    attendees: list[dict[str, Any]],
    *,
    users: list[UserRecord],
    alias_index: dict[str, list[AliasRecord]],
    meeting_id: str | None = None,
) -> list[Mapping]:
    """Map each attendee entry: email first, then the attendee name."""
    mappings: list[Mapping] = []
    for entry in attendees:
        email = entry.get("email")
        name = entry.get("name")
        if email:
            by_email = map_email(email, label=name or email, users=users)
            if by_email.state in RESOLVED_STATES or by_email.state == INACTIVE:
                mappings.append(by_email)
                continue
            # An email that matches nothing is still informative; fall through to
            # the name only to record the alias/name evidence alongside it.
            by_name = resolve_label(
                name, users=users, alias_index=alias_index, meeting_id=meeting_id
            )
            if by_name.state in RESOLVED_STATES:
                by_name.evidence = (
                    f"email {normalize_email(email)} matched no user; "
                    + by_name.evidence
                )
                mappings.append(by_name)
            else:
                mappings.append(by_email)
        else:
            mappings.append(
                resolve_label(
                    name, users=users, alias_index=alias_index, meeting_id=meeting_id
                )
            )
    return mappings


def map_labels(
    labels: Iterable[str | None],
    *,
    users: list[UserRecord],
    alias_index: dict[str, list[AliasRecord]],
    meeting_id: str | None = None,
) -> list[Mapping]:
    return [
        resolve_label(label, users=users, alias_index=alias_index, meeting_id=meeting_id)
        for label in labels
    ]


def summarise_mappings(mappings: list[Mapping]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for mapping in mappings:
        counts[mapping.state] = counts.get(mapping.state, 0) + 1
    return dict(sorted(counts.items()))


def unresolved(mappings: list[Mapping]) -> list[Mapping]:
    return [m for m in mappings if m.state not in RESOLVED_STATES]