"""Decision-useful synthesis.

The brief asks the summary to "explain the company, changes and unknowns without
making unsupported claims". Two of those three are about restraint, so this is
generated **deterministically from published claims** rather than by a model.

That is a deliberate trade. A template cannot hallucinate a revenue figure or
invent a director, it costs nothing per batch, and it produces byte-identical
output for an unchanged snapshot — which the idempotent-refresh gate requires
and a sampled model would quietly break.

Every sentence is built from a claim whose availability is `available`, and the
claim ids that produced it travel with the summary. What the agent could not
establish is stated with the reason it could not, because on this universe —
where most companies are dormant holding entities — the honest answer is often
"almost nothing is public", and saying so clearly is the useful answer.
"""

from __future__ import annotations

from typing import Any

from .models import Availability, Change, Claim, Envelope

# Norwegian legal forms, expanded for a reader who does not know the codes.
FORM_WORDS = {
    "AS": "a private limited company",
    "ASA": "a public limited company",
    "ANS": "a general partnership",
    "DA": "a partnership with shared liability",
    "ENK": "a sole proprietorship",
    "NUF": "a Norwegian branch of a foreign company",
    "SA": "a cooperative",
    "STI": "a foundation",
    "BRL": "a housing cooperative",
    "ESEK": "a jointly owned property",
    "FLI": "a voluntary association",
    "IKS": "an inter-municipal company",
}

CHANGE_WORDS = {
    "new_role": "{name} joined as {role}",
    "departed_role": "{name} left the role of {role}",
    "new_location": "a workplace was registered in {where}",
    "closed_location": "a registered workplace closed in {where}",
    "new_filing": "{field} moved from {old} to {new}",
    "changed_identity": "{field} changed from {old} to {new}",
    "changed_description": "the website description changed",
    "became_available": "{field} became available",
    "became_unavailable": "{field} is no longer reported",
    "source_unavailable": "{field} could not be re-read; the last known value is preserved",
}


def _money(value: Any, currency: str | None) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    unit = currency or "NOK"
    for scale, word in ((1e9, "bn"), (1e6, "m"), (1e3, "k")):
        if abs(number) >= scale:
            return f"{unit} {number / scale:,.1f}{word}".replace(",", " ")
    return f"{unit} {number:,.0f}".replace(",", " ")


class Synthesiser:
    """Turns an envelope's published claims into prose and an unknowns list."""

    def __init__(self, envelope: Envelope) -> None:
        self.envelope = envelope
        self.claims: dict[str, Claim] = {c.field: c for c in envelope.claims}
        self.used: list[str] = []

    # -- claim access ----------------------------------------------------

    def _get(self, field: str) -> Any:
        """A value only when the claim actually says `available`."""
        claim = self.claims.get(field)
        if not claim or claim.availability != Availability.AVAILABLE:
            return None
        if claim.value in (None, "", []):
            return None
        self.used.extend(claim.evidence_ids)
        return claim.value

    def _qualifier(self, field: str, key: str) -> Any:
        claim = self.claims.get(field)
        return claim.qualifiers.get(key) if claim else None

    # -- sections --------------------------------------------------------

    def identity(self) -> str:
        name = self._get("legal_name") or self.envelope.organisation_number
        form = str(self._get("legal_form") or "")
        code = form.split(" - ")[0].strip()
        words = FORM_WORDS.get(code, "a registered entity")
        industry = str(self._get("industry_code") or "")
        where = self._get("municipality")
        registered = self._get("registration_date")

        sentence = f"{name} is {words}"
        if where:
            sentence += f" in {str(where).title()}"
        if registered:
            sentence += f", registered {registered}"
        if industry and " " in industry:
            sentence += f". Its registered activity is {industry.split(' ', 1)[1].lower()}"
        return sentence + "."

    def standing(self) -> str | None:
        """Bankruptcy or liquidation leads, because it changes every other fact."""
        status = self._get("operating_status") or {}
        flags = [
            ("bankruptcy", "is registered as bankrupt"),
            ("compulsory_liquidation", "is under compulsory liquidation"),
            ("under_liquidation", "is being wound up"),
        ]
        hits = [words for key, words in flags if status.get(key)]
        if hits:
            return f"The company {hits[0]}."
        if status.get("in_group"):
            return "The registry records this entity as part of a group."
        return None

    def scale(self) -> str | None:
        revenue = self._get("financials.revenue")
        result = self._get("financials.net_result")
        period = (self.claims.get("financials.revenue") or Claim(
            field="x", availability=Availability.NOT_AVAILABLE)).reporting_period
        currency = self._qualifier("financials.revenue", "currency")
        statement = self._qualifier("financials.revenue", "statement_type")
        employees = self._get("employees_registered")

        parts: list[str] = []
        if revenue is not None:
            year = (period or "").split("/")[-1][:4]
            kind = "company accounts" if statement == "SELSKAP" else "group accounts"
            filed = f"Accounts filed for {year} report revenue of {_money(revenue, currency)}" if year \
                else f"Filed accounts report revenue of {_money(revenue, currency)}"
            if result is not None:
                filed += f" and a net result of {_money(result, currency)}"
            parts.append(f"{filed} ({kind}).")
            if self._qualifier("financials.revenue", "filed_zero"):
                parts.append("The filed revenue figure is zero, which is a reported value rather than a missing one.")
        if employees is not None:
            parts.append(f"{employees} employees are registered.")
        return " ".join(parts) or None

    def people(self) -> str | None:
        director = self._get("managing_director")
        chair = self._get("board_chair")
        leadership = self._get("leadership") or []
        board = [p for p in leadership if isinstance(p, dict)
                 and p.get("role_code") in {"MEDL", "LEDE", "NEST"}]
        if director and chair and str(director).strip() == str(chair).strip():
            # One person in both roles reads as a duplicate unless it is named.
            sentence = f"{director} is both registered managing director and board chair."
        else:
            parts = []
            if director:
                parts.append(f"{director} is the registered managing director")
            if chair:
                parts.append(f"{chair} chairs the board")
            if not parts:
                return None
            sentence = " and ".join(parts) + "."
        if len(board) > 1:
            sentence += f" The board has {len(board)} registered members."
        return sentence

    def presence(self) -> str | None:
        parts: list[str] = []
        website = self._get("official_website")
        if website:
            proof = self._qualifier("official_website", "identity_proof") or ""
            how = {
                "org_number_labelled_on_page": "the organisation number appears on the page",
                "org_number_on_page": "the organisation number appears on the page",
                "registry_site_corroborated": "a registry address or phone number appears on the page",
            }.get(proof, "the registry declares it")
            parts.append(f"Its verified website is {website}, tied to this entity because {how}.")

        rating = self._get("places.rating")
        count = self._get("places.rating_count")
        if rating is not None:
            tail = f" from {count} ratings" if count else ""
            parts.append(f"Google records a rating of {rating} out of 5{tail}.")

        platforms = sorted({o.platform for o in self.envelope.observations} - {"brreg"})
        if platforms:
            named = ", ".join(platforms[:-1]) + f" and {platforms[-1]}" if len(platforms) > 1 else platforms[0]
            parts.append(f"Confirmed presence on {named}.")
        return " ".join(parts) or None

    def activity(self) -> str | None:
        parts = []
        jobs = self.claims.get("jobs.active_count")
        if jobs and jobs.availability == Availability.AVAILABLE:
            self.used.extend(jobs.evidence_ids)
            parts.append(
                f"{jobs.value} active job adverts carry this organisation number."
                if jobs.value else
                "No active job advert in the national vacancy feed carries this organisation number."
            )
        latest = self._get("activity.latest_post_date")
        posts = self._get("activity.posts") or []
        if latest:
            noun = "post" if len(posts) == 1 else "posts"
            parts.append(
                f"The company published {len(posts)} dated {noun} of its own, "
                f"most recently on {latest}."
            )
        return " ".join(parts) or None

    def changes(self) -> str | None:
        material = [c for c in self.envelope.changes if c.materiality == "material"]
        if not material:
            return None
        return "Since the previous run: " + "; ".join(
            self._phrase(c) for c in material[:6]
        ) + "."

    @staticmethod
    def _phrase(change: Change) -> str:
        template = CHANGE_WORDS.get(change.change_type, "{field} changed")
        value = change.new_value if change.new_value is not None else change.old_value
        record = value if isinstance(value, dict) else {}
        return template.format(
            field=change.field.replace("_", " ").replace(".", " "),
            old=str(change.old_value)[:40], new=str(change.new_value)[:40],
            name=record.get("name", "a person"),
            role=str(record.get("role", "a role")).replace("_", " "),
            where=record.get("municipality") or record.get("name") or "a new location",
        )

    def unknowns(self) -> list[dict[str, str]]:
        """What we could not establish, with the reason. Stated, never implied."""
        interesting = (
            "official_website", "employees_registered", "financials.revenue",
            "managing_director", "locations", "places.rating", "jobs.active_count",
            "activity.latest_post_date", "wikidata.qid",
        )
        out = []
        for field in interesting:
            claim = self.claims.get(field)
            if not claim or claim.availability == Availability.AVAILABLE:
                continue
            out.append({
                "field": field,
                "state": str(claim.availability),
                "reason": claim.note or "no permitted source established this value",
            })
        return out

    def build(self) -> dict[str, Any]:
        sections = {
            "identity": self.identity(),
            "standing": self.standing(),
            "scale": self.scale(),
            "people": self.people(),
            "presence": self.presence(),
            "activity": self.activity(),
            "changes": self.changes(),
        }
        ordered = [v for v in sections.values() if v]
        unknowns = self.unknowns()
        # Checking the sections is not enough: `activity` says something even
        # when the only thing to say is that the vacancy feed found nothing.
        external = {o.platform for o in self.envelope.observations} - {"brreg"}
        if not external:
            ordered.append(
                "No permitted external source could be tied to this entity, so the "
                "profile rests on official records alone."
            )
        return {
            "text": " ".join(ordered),
            "sections": {k: v for k, v in sections.items() if v},
            "unknowns": unknowns,
            "evidence_ids": sorted(set(self.used)),
            "method": "deterministic_claim_template_v1",
            "claim_boundary": (
                "Generated only from claims marked available. No value here is "
                "inferred, estimated or model-generated."
            ),
        }


def summarise(envelope: Envelope) -> dict[str, Any]:
    return Synthesiser(envelope).build()
