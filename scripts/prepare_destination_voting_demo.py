"""Prepare an isolated development voting state for an existing trip.

Run with:
    PYTHONPATH=src uv run python scripts/prepare_destination_voting_demo.py --trip-id <trip-id>
"""

from __future__ import annotations

import argparse
import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import delete, select

from app.core.database import SessionLocal
from app.models.destination import (
    Destination,
    DestinationCandidate,
    DestinationVote,
    DestinationVotingState,
)
from app.models.member import TripMember
from app.models.trip import Trip
from app.models.user import User
from app.services.destination import (
    enrich_candidate_weather,
    start_voting,
    update_ballot,
)
from app.services.places import resolve_destination

DEMO_CANDIDATES = (
    (
        "Portland, Maine",
        "Maine, USA",
        "US",
        Decimal("825.00"),
        Decimal("0.95"),
        ["food", "coast", "culture"],
    ),
    (
        "Boston, Massachusetts",
        "Massachusetts, USA",
        "US",
        Decimal("910.00"),
        Decimal("0.88"),
        ["history", "food", "culture"],
    ),
    (
        "Bar Harbor, Maine",
        "Maine, USA",
        "US",
        Decimal("780.00"),
        Decimal("0.84"),
        ["coast", "nature", "photography"],
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare development destination voting data")
    parser.add_argument("--trip-id", required=True, type=uuid.UUID)
    parser.add_argument(
        "--leave-member", help="Trip member UUID or user email to leave without a vote"
    )
    parser.add_argument(
        "--start-date", type=date.fromisoformat, default=date.today() + timedelta(days=1)
    )
    parser.add_argument("--end-date", type=date.fromisoformat)
    return parser.parse_args()


def find_leave_member(members: list[tuple[TripMember, User]], value: str | None) -> TripMember:
    if value:
        for member, user in members:
            if str(member.id) == value or user.email == value:
                return member
        raise SystemExit(f"No eligible trip member matches {value}")
    return max((member for member, _user in members), key=lambda member: str(member.id))


def prepare(
    trip_id: uuid.UUID, leave_member: str | None, start_date: date, end_date: date | None
) -> None:
    end_date = end_date or start_date + timedelta(days=7)
    if start_date > end_date:
        raise SystemExit("The start date must be on or before the end date")
    with SessionLocal() as db:
        trip = db.get(Trip, trip_id)
        if trip is None:
            raise SystemExit(f"Trip {trip_id} was not found")
        members = list(
            db.execute(
                select(TripMember, User)
                .join(User, User.id == TripMember.user_id)
                .where(
                    TripMember.trip_id == trip_id,
                    TripMember.status == "ACTIVE",
                    TripMember.voting_eligible.is_(True),
                )
            ).all()
        )
        if len(members) < 3:
            raise SystemExit("The development trip needs at least three eligible members")
        leave = find_leave_member(members, leave_member)

        db.execute(delete(DestinationVote).where(DestinationVote.trip_id == trip_id))
        db.execute(delete(DestinationVotingState).where(DestinationVotingState.trip_id == trip_id))
        db.execute(delete(DestinationCandidate).where(DestinationCandidate.trip_id == trip_id))
        trip.selected_destination_id = None
        trip.voting_started_at = None
        trip.voting_closed_at = None
        trip.selected_start_date = start_date
        trip.selected_end_date = end_date
        trip.state = "PLANNING"

        candidates: list[DestinationCandidate] = []
        for name, location, country_code, cost, score, tags in DEMO_CANDIDATES:
            destination = db.scalar(select(Destination).where(Destination.name == name))
            if destination is None:
                destination = Destination(
                    name=name,
                    location=location,
                    country_code=country_code,
                    baseline_cost=cost,
                    tags=tags,
                )
                db.add(destination)
                db.flush()
            if destination.latitude is None or destination.longitude is None:
                resolved = resolve_destination(destination.name, destination.location)
                if resolved:
                    latitude = resolved.get("latitude")
                    longitude = resolved.get("longitude")
                    if isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
                        destination.latitude = Decimal(str(latitude))
                        destination.longitude = Decimal(str(longitude))
            candidate = DestinationCandidate(
                trip_id=trip_id,
                destination_id=destination.id,
                match_score=score,
                match_explanation="Development voting candidate for the selected destination test.",
                estimated_cost_per_person=cost,
                currency="USD",
                recommendation_data_status="calculated",
            )
            db.add(candidate)
            db.flush()
            enrich_candidate_weather(db, candidate, start_date, end_date, commit=False)
            candidates.append(candidate)

        db.commit()
        start_voting(db, trip_id, [member.id for member, _user in members])
        voters = [member for member, _user in members if member.id != leave.id]
        vote_targets = [candidates[0], candidates[0], candidates[1], candidates[2]]
        for index, member in enumerate(voters):
            target = vote_targets[index % len(vote_targets)]
            update_ballot(db, trip_id, member.id, [target.id])

        print(f"Prepared {len(candidates)} candidates for trip {trip_id}")
        print(f"Left unvoted member: {leave.id}")
        print("Vote state: first two voters Portland, next Boston, remaining Bar Harbor")


if __name__ == "__main__":
    arguments = parse_args()
    prepare(arguments.trip_id, arguments.leave_member, arguments.start_date, arguments.end_date)
