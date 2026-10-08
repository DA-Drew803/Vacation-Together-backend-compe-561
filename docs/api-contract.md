# Vacation Together Backend API Contract Draft

## Scope

This document defines the planned API for the collaborative Vacation Together MVP. It is a contract for implementation, not a description of endpoints that already exist.

The backend owns authentication, authorization, persistence, group-preference aggregation, destination matching, weather retrieval, deterministic destination voting, AI itinerary generation and revision, and deterministic group approval. The frontend owns presentation and user interaction.

The architecture is confirmed. Details under **Implementation dependencies and unresolved decisions** remain open and must not be invented.

## Confirmed architecture

- Every traveler has a User account and participates through TripMember.
- The Trip creator becomes OWNER.
- OWNER may create the Trip, create and share invitation links, and remove members.
- OWNER has no extra voting or itinerary-approval authority.
- A Trip has one shared final date range, budget per person, and currency.
- Every TripMember manages only their own interests and private free-text preferences.
- Raw private free text is not disclosed to other members by default.
- The MVP has no per-member Availability, per-member Budget, or lowest-member-budget logic.
- The backend aggregates preferences and generates weather-enriched DestinationCandidates.
- DestinationCandidates represent destination recommendations, not complete itineraries.
- All active members vote with equal weight.
- Deterministic backend logic selects the winning destination and sets `selected_destination_id`.
- Setting the selected destination automatically starts AI generation of a draft itinerary.
- Members review and may request approved structured revisions.
- Deterministic backend logic finalizes the itinerary only when the eventual group-approval rule is met.
- General chatbot behavior is separate.

## Responsibility boundaries

AI may interpret preference text, help generate destination explanations, generate a draft itinerary, and generate revised itinerary content.

AI must not fabricate weather, authenticate users, authorize actions, determine membership, count votes, select the winner, alter voting results, or decide whether group approval is satisfied.

Weather is retrieved and normalized by backend integration code from an approved online service. Voting and approval are deterministic backend operations.

## Domain model

| Model | Purpose |
| --- | --- |
| `User` | Identity and password-authentication record. |
| `Trip` | Shared dates, budget per person, currency, selected destination, and current lifecycle state. |
| `TripMember` | User membership in a Trip with `OWNER` or `MEMBER` role. |
| `Invitation` | Token and lifecycle metadata for joining a Trip. |
| `MemberPreference` | One member's interests and private free-text ideas for one Trip. |
| `Destination` | Stable destination identity and destination metadata. |
| `DestinationCandidate` | Trip-specific destination recommendation, match data, cost estimate, and weather snapshot/reference. |
| `DestinationVote` | A TripMember's vote for a Trip-scoped DestinationCandidate. Final constraints depend on voting rules. |
| `Itinerary` | Shared itinerary content, version, lifecycle status, and selected destination. |
| `ItineraryItem` | Structured day/activity item belonging to an Itinerary. |
| Approval representation | Member approval records or an equivalent normalized representation; exact model depends on the final approval rule. |

Required integrity includes unique membership per User and Trip, one MemberPreference per TripMember, Trip-scoped candidate/vote references, equal vote weight, and a selected destination belonging to the relevant Trip candidate set.

Do not create separate SuggestedPlan and DestinationCandidate models for the same pre-vote concept. A DestinationCandidate is not a generated itinerary.

## Permission summary

| Operation | OWNER | MEMBER | Non-member |
| --- | --- | --- | --- |
| Create a Trip | Yes | A creator becomes OWNER | Auth required |
| View own Trip | Yes | Yes | No |
| Create/share an invitation | Yes | No | No |
| Remove a member | Yes | No | No |
| View shared settings | Yes | Yes | No |
| Update shared settings | Unresolved | Unresolved | No |
| Manage own preferences | Yes | Yes | No |
| Manage another member's preferences | No | No | No |
| View DestinationCandidates | Yes | Yes | No |
| Vote | Yes, normal weight | Yes, normal weight | No |
| Override voting or set selected destination | No | No | No |
| View shared itinerary | Yes | Yes | No |
| Request revisions | Unresolved | Unresolved | No |
| Approve itinerary | Yes under group rule | Yes under group rule | No |
| Finalize approval directly | No | No | No |

## Endpoint surface

Paths and JSON fields use `snake_case`.

```text
POST   /api/auth/register
POST   /api/auth/login
GET    /api/auth/me

GET    /api/trips
POST   /api/trips
GET    /api/trips/{trip_id}

GET    /api/trips/{trip_id}/members
DELETE /api/trips/{trip_id}/members/{trip_member_id}

POST   /api/trips/{trip_id}/invitations
POST   /api/invitations/{invitation_token}/accept

GET    /api/trips/{trip_id}/settings
PATCH  /api/trips/{trip_id}/settings

GET    /api/trips/{trip_id}/preferences/me
PUT    /api/trips/{trip_id}/preferences/me

POST   /api/trips/{trip_id}/destination_candidates/generate
GET    /api/trips/{trip_id}/destination_candidates

GET    /api/trips/{trip_id}/votes/me
PUT    /api/trips/{trip_id}/votes/me
GET    /api/trips/{trip_id}/voting_state

GET    /api/trips/{trip_id}/itinerary
POST   /api/trips/{trip_id}/itinerary/revisions

GET    /api/trips/{trip_id}/itinerary/approval
PUT    /api/trips/{trip_id}/itinerary/approval/me
```

There is no endpoint for manual destination selection, direct client assignment of `selected_destination_id`, or user-triggered initial itinerary generation.

## Authentication

`POST /api/auth/register` creates a User using validated credentials and returns a safe User/session response. Passwords are hashed and never returned.

`POST /api/auth/login` validates credentials and issues the JWT used for protected requests. Protected requests send that JWT using the agreed authentication header. `GET /api/auth/me` validates the current JWT and returns the authenticated current User so the frontend can restore a session from its stored token. Expired, malformed, or invalid tokens receive `401 Unauthorized`.

The MVP uses stateless JWT authentication. Sign-out is client-side removal of the stored token and other local authentication state, so there is no `POST /logout` endpoint. Server-side logout would require a future token-revocation design. Token lifetime and any refresh-token strategy remain unresolved, and clients must not assume refresh tokens exist. Protected Trip endpoints resolve the authenticated User and then enforce active membership.

## Trips, membership, and invitations

`POST /api/trips` creates a Trip and atomically creates an OWNER TripMember for the authenticated creator. `GET /api/trips` lists Trips for the authenticated User. `GET /api/trips/{trip_id}` requires active membership.

`GET /api/trips/{trip_id}/members` returns safe membership information. It must not expose password, token, or private preference data.

`DELETE /api/trips/{trip_id}/members/{trip_member_id}` is an OWNER group-administration operation. Its effects on preferences, votes, and active approval remain unresolved.

`POST /api/trips/{trip_id}/invitations` allows OWNER to create and share an invitation link. `POST /api/invitations/{invitation_token}/accept` allows an authenticated invited User to join as MEMBER.

Invitation errors use neutral states: invalid, expired, revoked, unavailable, or already a member. The contract does not assume invitations are single-use.

## Shared Trip settings

`GET /api/trips/{trip_id}/settings` returns one shared date range, one budget per person, and currency.

Conceptual response:

```json
{
  "trip_id": "trip_123",
  "start_date": "2027-06-10",
  "end_date": "2027-06-17",
  "budget_per_person": {
    "amount": 950,
    "currency": "USD"
  }
}
```

`PATCH /api/trips/{trip_id}/settings` uses authorization rules still to be decided. Do not accept member-specific dates or budgets.

## Current-member preferences and privacy

`GET /api/trips/{trip_id}/preferences/me` and `PUT /api/trips/{trip_id}/preferences/me` operate only on the authenticated active TripMember's preferences.

Conceptual request:

```json
{
  "interests": ["food", "museums", "nature"],
  "free_text": "I prefer relaxed mornings and local restaurants."
}
```

No endpoint permits one member to edit another member's preferences. Raw free text is not included in other members' responses by default. Group-level aggregation may expose safe derived categories or summaries.

## Destination matching and weather

`POST /api/trips/{trip_id}/destination_candidates/generate` generates Trip-scoped destination recommendations after readiness and trigger rules are finalized.

`GET /api/trips/{trip_id}/destination_candidates` returns the same candidate collection to every active member.

A DestinationCandidate represents a destination recommendation, not a full itinerary. Conceptual response item:

```json
{
  "id": "candidate_123",
  "destination": {
    "id": "destination_tokyo",
    "name": "Tokyo"
  },
  "match_score": 0.91,
  "match_explanation": "Matches the group's food, museum, and city interests.",
  "estimated_cost_per_person": {
    "amount": 950,
    "currency": "USD"
  },
  "recommendation_data_status": "calculated",
  "weather": {
    "data_type": "forecast",
    "temperature_low": 52,
    "temperature_high": 63,
    "temperature_unit": "F",
    "condition": "partly_cloudy",
    "precipitation_probability": 30,
    "source": "weather_provider",
    "period_start": "2027-06-10",
    "period_end": "2027-06-17"
  }
}
```

The final schema may normalize provider metadata separately, but it must distinguish:

- `forecast`: forecast data within the provider's reliable horizon
- `historical_average`: historical observations summarized for a comparable period
- `seasonal_average`: broader seasonal climate information
- unavailable weather when no supported data exists

Historical or seasonal values must never be labeled as a forecast. Responses document temperature units, represented date/period, provider/source metadata when available, and missing fields. Provider failures return a documented partial or unavailable state; AI must not fill missing weather.

Provider selection, forecast horizon, fallback policy, and units remain implementation decisions. No provider or library is selected by this contract.

## Destination voting

`GET /api/trips/{trip_id}/votes/me` returns the authenticated member's ballot. `PUT /api/trips/{trip_id}/votes/me` creates or updates it only when finalized rules permit. `GET /api/trips/{trip_id}/voting_state` exposes allowed participation and result information.

Every active member has equal voting weight. A member cannot vote as another member. AI does not count votes or choose the winner.

When finalized deterministic rules identify the winner, the backend sets `Trip.selected_destination_id`. Clients cannot assign this field directly, and OWNER cannot override it.

The ballot schema remains flexible until the team decides whether one or multiple destination selections are allowed.

## Automatic draft itinerary generation

Setting `selected_destination_id` automatically starts AI draft-itinerary generation. There is no user-facing initial generation endpoint.

`GET /api/trips/{trip_id}/itinerary` returns generation and lifecycle status plus content when available. Initial generation supports at least pending, generating, completed, and failed transport states. The domain itinerary then progresses conceptually through draft, under review, revised, pending approval, and approved.

Conceptual generating response:

```json
{
  "trip_id": "trip_123",
  "selected_destination_id": "destination_tokyo",
  "generation_status": "generating",
  "itinerary": null,
  "error": null
}
```

Repeated retrieval must not create duplicate itineraries.

## Structured itinerary revisions

`POST /api/trips/{trip_id}/itinerary/revisions` accepts only approved structured requests, such as changing a selected activity, reducing walking for a selected day, or lowering a selected day's cost.

AI may generate revised content. Deterministic backend code validates membership, authorization, request type, lifecycle transition, persistence, and effects on approval state. Revision authorization, supported types, whether requests need group approval, and approval reset behavior remain unresolved.

## Group itinerary approval

`GET /api/trips/{trip_id}/itinerary/approval` returns the current member's allowed approval view and group progress. `PUT /api/trips/{trip_id}/itinerary/approval/me` records or updates only the authenticated member's approval state when permitted.

OWNER has no special approval weight. AI cannot decide approval. Deterministic backend logic changes the itinerary to approved/final only when the finalized group rule is satisfied.

The data model may use ItineraryApproval records or an equivalent normalized representation. Avoid a separate model if the final rule can be represented cleanly without losing member-level integrity.

## Error format

Use one documented error envelope with a stable code, English message, and optional field details.

| Status | Meaning |
| --- | --- |
| `400` | Invalid state transition or unsupported operation. |
| `401` | Missing, invalid, or expired authentication. |
| `403` | Authenticated but not an active member or lacking required administration permission. |
| `404` | Resource does not exist or is intentionally hidden. |
| `409` | Membership, invitation, ballot, lifecycle, or approval conflict. |
| `422` | Request validation failure. |
| `502` / `503` | Approved external AI or weather service unavailable. |

## Implementation dependencies and unresolved decisions

These decisions affect implementation but do not contradict the architecture.

### Invitations

- Single-use or reusable; expiration; revocation; acceptance/member limits; rejoining after removal.

### Shared settings

- Who proposes, edits, and finalizes dates and budget; included costs; currency behavior.

### Destination generation

- Readiness threshold; generation trigger; regeneration; treatment of existing votes.

### Weather

- Provider; reliable forecast horizon; historical/seasonal fallback; supported units and conversions.

### Voting

- Ballot cardinality; vote changes; closing; required participation; result visibility; ties; membership changes; removed-member votes; reopening.

### Revisions

- Authorization; supported types; whether revisions require approval; effects on existing approvals.

### Group approval

- Majority, unanimity, percentage, or another rule; required participation; approval reset after revision; finalization behavior.
