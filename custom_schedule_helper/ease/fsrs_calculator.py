from ..py_fsrs.fsrs import Scheduler, Card, State, Rating

from typing import NamedTuple, TypedDict
from datetime import datetime, timezone, timedelta
import math

LOG = False


class Review(NamedTuple):
    """A named tuple representing a review event."""

    rating: Rating
    revlog_id: int
    state: State
    step: int | None = None


class StepResults(TypedDict):
    """A dictionary representing the results of a review step."""

    revlog_id: int
    card_id: int
    state: str
    stability: float | None
    rating: int
    interval: float
    factor: int
    difficulty: float | None
    display_difficulty: float
    retrievability: float | None


FSRS5_DEFAULT_DECAY = 0.5
FSRS6_DEFAULT_DECAY = 0.1542

DEFAULT_PARAMETERS = [
    0.212,
    1.2931,
    2.3065,
    8.2956,
    6.4133,
    0.8334,
    3.0194,
    0.001,
    1.8722,
    0.1666,
    0.796,
    1.4835,
    0.0614,
    0.2629,
    1.6483,
    0.6014,
    1.8729,
    0.5425,
    0.0912,
    0.0658,
    FSRS6_DEFAULT_DECAY,
]


def migrateParameters(
    parameters: list[float] = None,
) -> list[float]:
    """Migrate parameters to the latest FSRS-6 format."""
    if parameters is None:
        return list(DEFAULT_PARAMETERS)
    if len(parameters) == 21:
        return parameters
    elif len(parameters) == 19:
        print("[FSRS-6]auto fill w from 19 to 21 length")
        # Extend parameters from length 19 to 21
        return list(parameters) + [0.0, FSRS5_DEFAULT_DECAY]
    elif len(parameters) == 17:
        # copy parameters list
        w = list(parameters)
        w[4] = round(w[5] * 2.0 + w[4], 8)
        w[5] = round(math.log(w[5] * 3.0 + 1.0) / 3.0, 8)
        w[6] = round(w[6] + 0.5, 8)
        print("[FSRS-6]auto fill w from 17 to 21 length")
        return w + [0.0, 0.0, 0.0, FSRS5_DEFAULT_DECAY]
    else:
        # To throw use "checkParameters"
        # ref: https://github.com/open-spaced-repetition/ts-fsrs/pull/174#discussion_r2070436201
        print("[FSRS]Invalid parameters length, using default parameters")
        return list(DEFAULT_PARAMETERS)


class FsrsCalculator:
    """Calculator for FSRS algorithm parameters."""

    def __init__(
        self,
        parameters: list[float] = DEFAULT_PARAMETERS,
        desired_retention: float = 0.9,
        enable_fuzzing: bool = False,
        maximum_interval: int = 3650,
        learning_steps: list[int] | None = [],
        relearning_steps: list[int] | None = [],
    ):
        """Initialize the calculator with parameters."""
        self.parameters = migrateParameters(parameters)
        self.desired_retention = desired_retention
        self.enable_fuzzing = enable_fuzzing
        self.maximum_interval = maximum_interval
        self.learning_steps = learning_steps or []
        self.relearning_steps = relearning_steps or []

        print(f"Initialized with parameters: {self.parameters}")
        print("Original parameters:", parameters)
        print(f"Desired retention: {self.desired_retention}")
        print(f"Maximum interval: {self.maximum_interval}")
        print(f"Enable fuzzing: {self.enable_fuzzing}")

    def calc_display_difficulty(self, d):
        """Calculate display difficulty percentage (0-100).
        Args:
            d: Difficulty value (float) 1-10
        Returns:
            float: Display difficulty percentage (0-100)
        """
        return ((d - 1.0) / 9.0) * 100.0

    def calc_difficulty_to_factor(self, d: float) -> int:
        """Convert difficulty to a factor in the format of 100-1100.
        Args:
            d: Difficulty value (float) 1-10
        Returns:
            int: Factor in the format of 100-1100
        """
        if d is None:
            return 1000
        if d < 1.0:
            d = 1.0
        factor = ((d - 1.0) / 9.0) * 1000.0 + 100.0
        if factor < 100:
            factor = 100
        elif factor > 1100:
            factor = 1100
        return int(factor)

    def calc_display_difficulty_to_factor(self, display_difficulty: float) -> int:
        """Convert display difficulty percentage to a factor in the format of 100-1100.
        Args:
            display_difficulty: Display difficulty percentage (0-100)
        Returns:
            int: Factor in the format of 100-1100
        """
        if display_difficulty < 0.0:
            display_difficulty = 0.0
        if display_difficulty > 100.0:
            display_difficulty = 100.0
        return int(display_difficulty * 10 + 100)

    def steps(self, card_id: int, reviews: list[Review]) -> list[StepResults]:
        """Calculate review steps based on a sequence of ratings."""
        fsrs_card = Card(
            card_id=card_id,
            state=State.Learning,
            step=1,
            stability=None,
            difficulty=None,
            due=datetime.now(timezone.utc),
            last_review=None,
        )
        results = []

        scheduler = Scheduler(
            parameters=self.parameters,
            desired_retention=self.desired_retention,
            maximum_interval=self.maximum_interval,
            enable_fuzzing=self.enable_fuzzing,
            learning_steps=self.learning_steps,
            relearning_steps=self.relearning_steps,
        )

        for i, review in enumerate(reviews):
            fsrs_card.state = review.state
            # Convert epoch milliseconds to datetime
            timestamp = datetime.fromtimestamp(review.revlog_id / 1000, tz=timezone.utc)
            if LOG:
                print(
                    f"\nRev {i + 1} cid: {card_id} at:"
                    f" {timestamp.strftime('%Y-%m-%d %H:%M')}, rt: {review.rating.value}"
                )
            retrievability = scheduler.get_card_retrievability(
                fsrs_card,
                timestamp,
            )
            rating = review.rating
            new_card, _ = scheduler.review_card(
                fsrs_card,
                rating=rating,
                review_datetime=timestamp,
            )
            prev_last_review = fsrs_card.last_review
            fsrs_card = new_card
            # Update card's last review time
            fsrs_card.last_review = timestamp

            # Calculate interval from due date
            if fsrs_card.due is None:
                interval = 0.0
            else:
                interval = (fsrs_card.due - timestamp).total_seconds() / 86400.0  # Convert to days

            # Calculate display difficulty
            display_difficulty = self.calc_display_difficulty(fsrs_card.difficulty)
            factor = self.calc_display_difficulty_to_factor(display_difficulty)
            time_since_last_review = (
                (timestamp - prev_last_review).total_seconds() / 86400.0
                if prev_last_review
                else 0.0
            )
            if LOG:
                print(
                    f"Card {card_id}: disp D: {round(display_difficulty, 3)}, R:"
                    f" {round(retrievability, 3)}, I: {round(interval, 3)}, F: {round(factor, 3)},"
                    f" T: {round(time_since_last_review, 3)}, S: {round(fsrs_card.stability, 3)},"
                    f" raw D: {round(fsrs_card.difficulty, 3)}, last_review:"
                    f" {prev_last_review.isoformat() if prev_last_review else 'None'}"
                )

            # Create result object
            result = {
                "revlog_id": int(review.revlog_id),
                "card_id": fsrs_card.card_id,
                "state": fsrs_card.state.value,
                "stability": fsrs_card.stability,
                "difficulty": fsrs_card.difficulty,
                "rating": rating.value,
                "interval": interval,
                "factor": factor,
                "display_difficulty": display_difficulty,
                "retrievability": retrievability,
            }

            results.append(result)

        return results


if __name__ == "__main__":
    # Example usage
    card_id = 1
    card = Card(
        card_id=card_id,
        state=State.Learning,
        step=None,
        stability=None,
    )
    first_review_time = datetime.now(timezone.utc)
    second_review_time = first_review_time + timedelta(days=1)
    third_review_time = second_review_time + timedelta(days=3)
    fourth_review_time = third_review_time + timedelta(days=7)
    reviews = [
        Review(Rating.Good, first_review_time, State.Learning),
        Review(Rating.Good, second_review_time, State.Learning),
        Review(Rating.Good, third_review_time, State.Review),
        Review(Rating.Good, fourth_review_time, State.Review),
    ]

    calculator = FsrsCalculator()
    results = calculator.steps(card_id, reviews)
    for result in results:
        print(result)
