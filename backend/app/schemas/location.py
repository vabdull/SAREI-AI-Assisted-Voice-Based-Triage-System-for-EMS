"""Canonical patient-location schema used across the EMS backend.

The live transcript pipeline, the Case DB row, and every portal share a single
structured field (`patient_location`). Historical `location_text` /
`location_lat` / `location_lon` fields have been collapsed into this model so
there is one source of truth for "where the emergency is".

Shape (per cross-cutting spec):
    raw_text: str
    source_span: { start: int, end: int } | null
    components: { street, district, city, landmark, governorate } (all optional)
    geocode: { lat, lng, confidence, provider, match_type } | null
    confidence: 0.0..1.0     (extraction / merged confidence)
    needs_confirmation: bool (dispatcher must confirm?)

`source_span` is optional so this model can also represent a dispatcher-typed
address that has no transcript evidence attached.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class LocationSourceSpan(BaseModel):
    """Character-offset anchor into the live transcript."""

    model_config = ConfigDict(extra="ignore")

    start: int = Field(ge=0, description="Inclusive character offset into the transcript.")
    end: int = Field(ge=0, description="Exclusive character offset into the transcript.")


class LocationComponents(BaseModel):
    """Best-effort structured decomposition of the address."""

    model_config = ConfigDict(extra="ignore")

    street: Optional[str] = None
    district: Optional[str] = None
    city: Optional[str] = None
    landmark: Optional[str] = None
    governorate: Optional[str] = None


class LocationGeocode(BaseModel):
    """Forward-geocoding result attached to the location, when available."""

    model_config = ConfigDict(extra="ignore")

    lat: Optional[float] = None
    lng: Optional[float] = None
    confidence: float = 0.0
    provider: Optional[str] = None
    match_type: Optional[str] = None


class PatientLocation(BaseModel):
    """Canonical location payload.

    When unknown, callers should use `None` instead of constructing an empty
    `PatientLocation` (the DB column / API field is nullable).
    """

    model_config = ConfigDict(extra="ignore")

    raw_text: str = Field(description="Best-effort human-readable address or transcript phrase.")
    source_span: Optional[LocationSourceSpan] = None
    components: LocationComponents = Field(default_factory=LocationComponents)
    geocode: Optional[LocationGeocode] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_confirmation: bool = True
