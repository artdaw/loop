# Weather from multiple sources, with local preference

**Pack:** weather.local · **Version:** 1.0.0 · **Status:** normative target,
not a live integration. It replaces the single-provider assumption while keeping
the weather.forecast operation name used by routines and travel.

Use the [shared extension contract](README.md) and [starter template](template/README.md).
Read [runtime](../runtime.md), [interfaces](../interfaces.md),
[travel](travel.md), and [acceptance](../acceptance.md).

## 1. Outcome and meaning of local

“Check the weather before I leave. Compare sources and prefer local forecasts.”

Loop returns a location/time-specific briefing: expected temperature and feels-like,
precipitation timing, wind/gusts, applicable official warnings, practical preparation,
sources and update times. It explains material disagreement and missing coverage.
It can compare a home location, commute window or selected trip segment.

Local means the meteorological authority/products covering the requested geography,
plus representative nearby observations and radar. It does not mean whichever
weather website happens to use the local language, nor that all data is downloaded
without network access. Personal reasoning remains local-only under normal policy.

Freshness, product suitability and actual coverage are required before locality
is used as a preference. A stale local forecast does not outrank a fresh suitable
fallback. A nearby valley station is not automatically representative of a mountain
destination. Do not claim local sources are universally more accurate.

Daily-life owns the brief/advice; Seeker can resolve source documentation or gaps.
Collection, normalization, comparison and ordinary change detection are deterministic.
A local model may phrase the result within a one-call default; raw values, provenance,
warnings and uncertainty must remain unchanged. No model call is needed when a
template can render the result.

## 2. Registration and operations

Abridged registry overview; a runnable pack supplies the full manifest, schemas,
bindings and examples required by the [extension contract](README.md).

```yaml
schema_version: 1
id: weather.local
version: "1.0.0"
owner_role: daily_life
support_roles: [seeker]
policy_namespace: weather
capabilities:
  - weather.forecast
  - weather.prepare
  - weather.sources
dependencies:
  required: {}
  optional:
    weather.source.dwd: ">=1.0.0,<2.0.0"
    weather.source.meteoswiss: ">=1.0.0,<2.0.0"
    weather.source.geosphere: ">=1.0.0,<2.0.0"
    weather.source.open_meteo: ">=1.0.0,<2.0.0"
defaults:
  enabled: false
  budget_class: background
effects: [network_read, local_artifact_write, owner_notification_proposal]
```

Provider IDs above are intended adapter bindings, not claims that they are installed.
At least one relevant healthy provider is required to produce a forecast; the
registry can discover weather.sources without a network or configured provider.

- weather.forecast: WeatherRequest → WeatherBundle. Resolves eligible sources,
  fetches bounded data, normalizes, reconciles and persists evidence. Supports
  existing {location_ref,horizon_hours} calls with the defaults below.
- weather.prepare: WeatherRequest plus optional activity context_ref and
  bundle_id → WeatherBrief. Reuses a still-valid bundle or calls forecast through
  the shared executor. Returns advice and notification proposals, never sends itself.
- weather.sources: {location_ref?} → source descriptions, origin/lineage, coverage,
  status and selection reasons. Local metadata inspection, no model/network required.

WeatherRequest: schema_version defaults to 1; exactly one location_ref or explicit location
{latitude,longitude,timezone,elevation_m?}; either window_start/window_end UTC
timestamps or horizon_hours (default 3 from current clock); purposes array from
departure/outdoor/travel/general; source_ids optional subset of configured IDs;
compare=true by default. Window must be positive and supported ranges validated.
Explicit future windows outside available forecast horizons return missing coverage,
not invented forecasts. Missing location follows needs_input; timezone never
determines assumed coordinates. A supplied bundle must match request location,
time window, input labels and current policy revision before reuse.

All effects, dependencies, root budgets, retries, privacy, request idempotency,
cancellation and output validation use the generic capability executor.
Register source adapters through their manifests; selecting a new installed
source in vault policy requires no changes to Daily-life, travel, bot or scheduler.

## 3. Source catalogue and selection

SourceDescriptor fields: id, adapter_version, publisher, transport_provider,
product, authority (official/qualified_other/unknown), jurisdiction,
coverage_geometry, spatial_resolution?, elevation_range?, supported_variables,
supported_horizon, product_type (observation/radar/nowcast/forecast/warning),
model_family?, model_run_id?, lineage_ids[], update_interval_seconds?,
max_issue_age_seconds, permitted_endpoint_refs, attribution, terms_url,
credential_ref?, health. Domain metadata is validated by the adapter and reviewed
catalogue, not accepted from model prose. Unknown metadata stays unknown.

Selection is per product/variable/time window:
1. Applicable official warnings from the responsible jurisdiction.
2. Suitable local observations/radar/nowcasts for current conditions and near-term
   precipitation, where available. Observations alone do not forecast later weather.
3. Official local/national forecasts with the relevant geographic/terrain coverage.
4. Other suitable regional or independent forecast products for comparison.
5. A configured global aggregator/model fallback for uncovered variables/horizons.

Within a tier, rank coverage/terrain fit, explicit approved user priority, then
freshness and source ID. Compare up to three suitable forecast products; reserve
a separate warning check. Prefer distinct known origins for comparison. Limit
the whole call to six provider requests and the root budget, whichever is lower;
reuse caches and report sources not checked. A provider request may return several
products, but each product retains its own provenance.

Prefer direct official data when a supported adapter exists. A local-origin model
served by an aggregator remains useful and must name both publisher and transport.
Same model/product/run via different websites is one evidence family, not independent
votes. Multi-model blends disclose their lineage; unknown/correlated origins cannot
justify claims of independent agreement.

Initial geographic candidates, verified in official documentation on 2026-09-05:
- Germany: DWD documents MOSMIX forecasts, CAP warning formats and radar products.
  Implement the relevant subsets using its published schemas, not assumed JSON.
  [DWD Open Data documentation](https://www.dwd.de/DE/leistungen/opendata/hilfe.html?lsbId=627548).
- Switzerland: MeteoSwiss publishes local forecast data with downloadable parameter
  files and point metadata. The adapter must honor its time-aggregation definitions
  and attribution. [MeteoSwiss local forecast documentation](https://opendatadocs.meteoswiss.ch/e-forecast-data/e4-local-forecast-data).
- Austria: GeoSphere publishes an official warning API. Its coverage and projection
  must be normalized before matching locations; this endpoint alone does not provide
  every forecast variable. [GeoSphere warning API](https://openapi.hub.geosphere.at/warnapi/v1/).
- Fallback/comparison: Open-Meteo exposes DWD ICON products, so a DWD-based response
  is not automatically independent of another DWD product. Its generic forecast
  adapter remains available for suitable coverage.
  [Open-Meteo DWD API](https://open-meteo.com/en/docs/dwd-api).

These are adapter candidates, not guaranteed credentials, current site uptime,
or a “best service” ranking. An initial German deployment SHOULD implement DWD
forecast/warnings and a configured comparator/fallback. Other destinations select
the appropriate enabled catalogue entries, never hardcode Germany for every trip.
Provider endpoints, formats, rates and terms are verified again when implementing
an adapter. Preserve required attribution; do not copy restricted weather icons.
Sources beyond this catalogue are added with the same source adapter contract.

## 4. Normalization, age and comparison

WeatherSample: source_id, product_type, issued_at?, observed_at?, fetched_at,
valid_from, valid_to, location/geometry, elevation?, variable, value?, unit,
statistic (instant/mean/min/max/sum/probability), interval_seconds,
phenomenon?, threshold?, ensemble_member_or_quantile?, missing_reason?,
evidence_ref, model/run/lineage references and privacy.

Canonical units: Celsius, km/h, millimeters, probability percent. Keep originals
for audit; distinguish sustained wind from gusts, temperature from feels-like,
and rain from all-precipitation amount. Kelvin/Fahrenheit and m/s conversions
are deterministic. Missing feels-like remains null unless a documented deterministic
formula is explicitly enabled and labelled derived.

Represent provider accumulation intervals accurately, including end-labelled times.
Do not divide a three-hour rain probability into hourly probabilities, average
hourly probabilities into a whole-trip risk, treat probability as rainfall amount,
or compare unequal accumulation windows as equivalent. Accumulated amounts may
sum only complete non-overlapping intervals; missing intervals remain missing.
Do not silently interpolate rainfall, warning area or certainty from a radar image.
Unsupported image extraction yields an unavailable product.

Transport-cache ceilings: forecast 60 minutes; observations/nowcast/radar 10 minutes;
warnings 5 minutes or earlier publisher expiry. A valid sample must also have
provider issue/observation age within that product's declared max_issue_age_seconds
and cover the requested time. Polling an unchanged old model run does not make it
new. Unknown issued_at/age yields uncertain freshness and cannot satisfy verified
current coverage. Adapter metadata defines max issue age from its documented
publication cadence; never apply a universal one-hour model-run limit.

Compare only aligned variable/statistic/location/interval records with matching
phenomenon, probability threshold and ensemble statistic where applicable. Select the best
qualified primary per field and display the alternatives/range alongside it.
Do not silently average several forecasts into a made-up “consensus probability”.
Default disagreement thresholds, configurable product heuristics:
- Temperature/feels-like spread >=3°C.
- Same-window rain probability spread >=30 percentage points.
- Sustained wind or separately compared gust spread >=15 km/h.
- Source results imply different preparation actions within the requested window.

One suitable source yields single_source, not corroborated confidence. Multiple
transport feeds with one lineage yield correlated_sources. Material discrepancies
yield disagreement even if the primary is preferred. Other coverage states are
compared, partial, stale and unavailable; these are evidence states, not calibrated
probabilities of correctness. A fresh fallback can cover a variable while the
warning channel remains unavailable; preserve this distinction.

## 5. Brief, official warnings and practical preparation

WeatherBundle fields: id, schema_version, request, generated_at, policy_revision,
samples[], sources_attempted[], selected_by_field[], comparisons[], coverage_by_product,
warnings[], hourly_projection[], source_errors[], overall_status, privacy, expires_at.
overall_status is available/degraded/unavailable; a usable forecast with missing
requested alerts/comparison is degraded. expires_at is the earliest relevant cache/
product expiry; retain per-product status so one expired optional field cannot be
presented as fresh or erase all healthy fields.

WeatherBrief adds headline, local time window, current_conditions?,
forecast_summary[], preparation[], uncertainty[], source_links, checked_at,
bundle_id, next_recheck_at? and notification_candidates[]. A statement saying rain
is likely must identify the supporting source/time; conflicting evidence remains
visible. An example format (synthetic, not a current forecast):
“Local forecast suggests rain during departure; another model is drier. Take a
waterproof layer. Sources checked at 07:20; timing is uncertain.”

Preparation uses the existing temperature/rain/wind rules in interfaces, applied
to valid source-backed fields. If a qualified comparator crosses an action threshold
and the primary does not, give conditional advice with attribution; do not silently
change the primary probability or assert consensus. Distinguish wet-weather advice
from confirmed rain when precipitation type is unknown. No user wardrobe/medical
needs are inferred. Missing weather means unknown, not “no rain”.

Warning fields: publisher, provider_alert_id, message_id, update/cancel references,
event_type, original severity/urgency/certainty, normalized severity?, geometry,
effective_at, onset_at?, expires_at, instruction, source_url, status, fetched_at.
Preserve official wording/meaning and attribution; do not generate new official
warnings from model text. Keep updates and cancellations under the same alert
identity; independently issued warnings remain separately attributable.

Match warning geometry and validity to the actual point/route time, including
coordinate-system conversion. Never average away an official warning because
other forecasts look mild. An empty fresh complete official feed permits “no
active warnings in this checked feed”, not “safe”. Missing/stale/partial warning
data cannot establish no warnings. Explicit cancellation or documented full
snapshot semantics are needed to clear an earlier alert; silence during an outage
cannot cancel it.

Warning issue age alone does not cancel an alert: a still-effective warning can
remain active across multiple fresh feed checks. Use official validity/lifecycle
and successful feed freshness, not the forecast model-run age rule, for alert state.

Briefing delivery uses existing requested routines and Notification Manager.
New immediate official-warning subscriptions need explicit activation, including
location, categories, expiry and any quiet-hour exception. An official label alone
cannot widen notification authority. Repeated weather/travel routines deduplicate
the same warning per publisher + alert identity + material revision + destination;
severity increases, changed instructions or cancellations may warrant an update.

## 6. Daily-life, travel and vault integration

The existing departure routine still calls weather.forecast, now resolved to this
pack, and receives a bundle whose canonical hourly projection is available where
source intervals permit it. The departure-weather renderer understands per-field
source, missing values, comparison states and warning availability.
Every numeric projected value carries its source/interval; unsupported hourly
probability remains null. Budget includes nested provider calls.

Travel requests weather for each relevant destination and time segment, sharing
cached bundles. Home weather does not stand in for a destination forecast.
Sources are selected again as jurisdiction/terrain changes. Beyond forecast
coverage, return no current forecast and optionally sourced seasonal context.
Fresh evidence may propose moving an outdoor activity under travel's existing
revision and monitoring authority; it cannot change a booking or chosen itinerary.

Persist bundles/evidence as labelled versioned artifacts and expiring observations.
Official alert state is a versioned capability object, using the shared store.
Keys include source/product/run, normalized location, interval and requested fields;
cache labels propagate and cannot be reused across incompatible privacy scopes.
Repeated requests may reuse data but must preserve issued_at and actual age.

Daily forecasts and live warnings are operational state, not timeless wiki facts.
An optional operational summary belongs in 4-journal/loop, not compiler briefs.
A request to remember a durable weather-related finding or a personal observation
uses raw capture → ledger → compile with explicit time/place attribution.
Source/preferences changes live in vault policy; choosing a forecast once or
dismissing one message does not silently rewrite source rankings.

## 7. Interfaces and editable policy

Generic capability invocation works immediately after a pack is implemented and
enabled. Optional target shortcuts: loop weather LOCATION [--hours N] [--compare],
loop weather sources [--location LOCATION]; Telegram /weather <place or request>.
The configured default location may be used when omitted; otherwise needs_input.
HTTP convenience POST /api/v1/weather/briefings {request,context_ref?} and
GET /api/v1/weather/sources?location_ref=... invoke the same registry operations.
They follow shared authentication, JSON/idempotency, async run and error contracts.
Adding another provider needs no new user command or HTTP route.

Proposed _ctx/loop/behavior.md weather policy:

```yaml
weather:
  prefer_local: true
  compare: true
  default_horizon_hours: 3
  max_forecast_products: 3
  max_provider_requests: 6
  cache_ttl_seconds:
    forecast: 3600
    observation: 600
    nowcast: 600
    radar: 600
    warning: 300
  disagreement:
    temperature_c: 3
    rain_probability_points: 30
    wind_kph: 15
  source_priority_by_country:
    DE: [dwd, open_meteo]
    CH: [meteoswiss, open_meteo]
    AT: [geosphere, open_meteo]
  warning_delivery:
    enabled: false
    quiet_hours_override: false
```

Source IDs resolve to validated source descriptors and registered adapters; listing
an ID does not install or enable it. Unsupported source IDs are validation findings.
Country priority applies only to products that source actually supplies and covers.
Outside the explicit map use responsible-authority coverage metadata and configured
fallbacks. Paid feeds require deployment credentials and budget authority; keys
never appear in the vault. Changing a preferred installed source is a policy edit;
adding a new data format requires one adapter plus its parser/conformance fixtures.

Definition of done: WF01–WF24 in [acceptance](../acceptance.md), with synthetic
multiple sources, differing model lineage, unit/time normalization, fresh/stale
products, partial outages, warning lifecycle and destination changes. Live source
connections are optional and truthfully reported; the shared comparison and
fallback behavior is mandatory for specification conformance.
