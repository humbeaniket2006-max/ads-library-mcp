"""Input validation. Unknown fields are rejected; all strings are length-capped and control-char free."""
from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, field_validator

# ISO 3166-1 alpha-2 allowlist (officially assigned codes).
COUNTRIES = frozenset(
    ["AD", "AE", "AF", "AG", "AI", "AL", "AM", "AO", "AQ", "AR", "AS", "AT", "AU", "AW", "AX", "AZ", "BA", "BB", "BD", "BE", "BF", "BG", "BH", "BI", "BJ", "BL", "BM", "BN", "BO", "BQ", "BR", "BS", "BT", "BV", "BW", "BY", "BZ", "CA", "CC", "CD", "CF", "CG", "CH", "CI", "CK", "CL", "CM", "CN", "CO", "CR", "CU", "CV", "CW", "CX", "CY", "CZ", "DE", "DJ", "DK", "DM", "DO", "DZ", "EC", "EE", "EG", "EH", "ER", "ES", "ET", "FI", "FJ", "FK", "FM", "FO", "FR", "GA", "GB", "GD", "GE", "GF", "GG", "GH", "GI", "GL", "GM", "GN", "GP", "GQ", "GR", "GS", "GT", "GU", "GW", "GY", "HK", "HM", "HN", "HR", "HT", "HU", "ID", "IE", "IL", "IM", "IN", "IO", "IQ", "IR", "IS", "IT", "JE", "JM", "JO", "JP", "KE", "KG", "KH", "KI", "KM", "KN", "KP", "KR", "KW", "KY", "KZ", "LA", "LB", "LC", "LI", "LK", "LR", "LS", "LT", "LU", "LV", "LY", "MA", "MC", "MD", "ME", "MF", "MG", "MH", "MK", "ML", "MM", "MN", "MO", "MP", "MQ", "MR", "MS", "MT", "MU", "MV", "MW", "MX", "MY", "MZ", "NA", "NC", "NE", "NF", "NG", "NI", "NL", "NO", "NP", "NR", "NU", "NZ", "OM", "PA", "PE", "PF", "PG", "PH", "PK", "PL", "PM", "PN", "PR", "PS", "PT", "PW", "PY", "QA", "RE", "RO", "RS", "RU", "RW", "SA", "SB", "SC", "SD", "SE", "SG", "SH", "SI", "SJ", "SK", "SL", "SM", "SN", "SO", "SR", "SS", "ST", "SV", "SX", "SY", "SZ", "TC", "TD", "TF", "TG", "TH", "TJ", "TK", "TL", "TM", "TN", "TO", "TR", "TT", "TV", "TW", "TZ", "UA", "UG", "UM", "US", "UY", "UZ", "VA", "VC", "VE", "VG", "VI", "VN", "VU", "WF", "WS", "YE", "YT", "ZA", "ZM", "ZW"]
)
PLATFORMS = ("facebook", "instagram", "audience_network", "messenger", "whatsapp", "oculus", "threads",
             "streaming_services")
MEDIA = ("all", "image", "meme", "video", "none")
STATUSES = ("active", "inactive", "all")
AD_TYPES = ("all", "political")
DETAILS = ("compact", "full")

_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁠-⁤⁦-⁩﻿]")
_LANG = re.compile(r"^[a-z]{2,3}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _clean(v: str) -> str:
    if _CTRL.search(v):
        raise ValueError("control characters are not allowed")
    return v.strip()


Text100 = Annotated[str, StringConstraints(max_length=100)]


def _int_to_str(v):
    """LLM clients often send numeric IDs as JSON numbers."""
    return str(v) if isinstance(v, int) and not isinstance(v, bool) else v


PageId = Annotated[str, BeforeValidator(_int_to_str), StringConstraints(pattern=r"^\d{1,20}$")]


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def _no_ctrl(cls, v):
        if isinstance(v, str):
            return _clean(v)
        if isinstance(v, list):
            return [_clean(x) if isinstance(x, str) else x for x in v]
        return v


def _countries(v: list[str]) -> list[str]:
    out = []
    for c in v:
        c = c.upper()
        if c not in COUNTRIES:
            raise ValueError(f"'{c[:8]}' is not a supported ISO 3166-1 alpha-2 country code")
        if c not in out:
            out.append(c)
    return out


def _date(v: str | None) -> str | None:
    if v is None or v == "":
        return None
    if not _DATE_RE.match(v):
        raise ValueError("dates must be YYYY-MM-DD")
    date.fromisoformat(v)  # raises ValueError on e.g. 2024-02-31
    return v


class SearchAds(_Base):
    countries: Annotated[list[str], Field(min_length=1, max_length=5)]
    query: Text100 | None = None
    page_ids: Annotated[list[PageId], Field(max_length=10)] | None = None
    status: Literal["active", "inactive", "all"] = "active"
    ad_type: Literal["all", "political"] = "all"
    date_from: str | None = None
    date_to: str | None = None
    platforms: Annotated[list[Literal[*PLATFORMS]], Field(max_length=8)] | None = None
    languages: Annotated[list[str], Field(max_length=5)] | None = None
    media: Literal["all", "image", "meme", "video", "none"] | None = None
    limit: Annotated[int, Field(ge=1)] = 10
    cursor: Annotated[str, StringConstraints(max_length=600)] | None = None
    detail: Literal["compact", "full"] = "compact"
    include_links: bool = False

    @field_validator("countries")
    @classmethod
    def _c(cls, v):
        return _countries(v)

    @field_validator("date_from", "date_to")
    @classmethod
    def _df(cls, v):
        return _date(v)

    @field_validator("platforms", mode="before")
    @classmethod
    def _lower_platforms(cls, v):
        return [p.lower() for p in v] if isinstance(v, list) else v

    @field_validator("languages")
    @classmethod
    def _langs(cls, v):
        if v is None:
            return v
        v = [x.lower() for x in v]
        if not all(_LANG.match(x) for x in v):
            raise ValueError("languages must be ISO 639 codes like en or fr")
        return v

    def check(self) -> SearchAds:
        if not (self.query or self.page_ids):
            raise ValueError("provide query or page_ids")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be after date_to")
        return self


class FindAdvertiser(_Base):
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    countries: Annotated[list[str], Field(min_length=1, max_length=5)]

    @field_validator("countries")
    @classmethod
    def _c(cls, v):
        return _countries(v)


class SummarizeAdvertiser(_Base):
    page_id: PageId
    countries: Annotated[list[str], Field(min_length=1, max_length=5)]
    status: Literal["active", "inactive", "all"] = "all"

    @field_validator("countries")
    @classmethod
    def _c(cls, v):
        return _countries(v)


TOOL_PARAMS: dict[str, frozenset[str]] = {
    "search_ads": frozenset(SearchAds.model_fields),
    "find_advertiser": frozenset(FindAdvertiser.model_fields),
    "summarize_advertiser": frozenset(SummarizeAdvertiser.model_fields),
    "get_status": frozenset(),
}


def first_error(exc) -> str:
    """One short sentence from a pydantic ValidationError (never echoes the full input)."""
    err = exc.errors()[0]
    loc = ".".join(str(x) for x in err.get("loc", ()) if not isinstance(x, int)) or "input"
    msg = str(err.get("msg", "invalid")).removeprefix("Value error, ")
    return f"Invalid {loc}: {msg[:120]}."
