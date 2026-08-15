"""T19: point-level tennis match simulator (best of 3, working implementation)."""

from __future__ import annotations

import random

from ..base import BaseAgent
from ...models.output import AgentOutput, Status


def _play_game(p_serve: float, rng: random.Random) -> bool:
    """True if server holds. Standard game with deuce."""

    s = 0
    r = 0
    while True:
        if rng.random() < p_serve:
            s += 1
        else:
            r += 1
        if s >= 4 and s - r >= 2:
            return True
        if r >= 4 and r - s >= 2:
            return False


def _play_tiebreak(
    pa_serve: float,
    pb_serve: float,
    rng: random.Random,
    first_server_a: bool,
    target: int = 7,
) -> bool:
    """True if player A wins the tiebreak."""

    a = 0
    b = 0
    serving_a = first_server_a
    point_no = 0
    while True:
        p = pa_serve if serving_a else pb_serve
        won_by_server = rng.random() < p
        if (serving_a and won_by_server) or ((not serving_a) and (not won_by_server)):
            a += 1
        else:
            b += 1

        point_no += 1

        # Serve rotation: 1 point, then every 2 points.
        if point_no == 1 or (point_no > 1 and (point_no - 1) % 2 == 0):
            serving_a = not serving_a

        if a >= target and a - b >= 2:
            return True
        if b >= target and b - a >= 2:
            return False


def _play_set(
    pa_serve: float,
    pb_serve: float,
    rng: random.Random,
    a_serves_first: bool,
) -> tuple[bool, int, bool]:
    """Returns (a_won_set, total_games, a_serves_first_next_set)."""

    a = 0
    b = 0
    serving_a = a_serves_first

    while True:
        if a == 6 and b == 6:
            a_won = _play_tiebreak(pa_serve, pb_serve, rng, serving_a)
            return a_won, 13, not serving_a

        server_holds = _play_game(pa_serve if serving_a else pb_serve, rng)
        if (serving_a and server_holds) or ((not serving_a) and (not server_holds)):
            a += 1
        else:
            b += 1
        serving_a = not serving_a

        if a >= 6 and a - b >= 2:
            return True, a + b, serving_a
        if b >= 6 and b - a >= 2:
            return False, a + b, serving_a


def simulate_match(
    pa_serve: float,
    pb_serve: float,
    n: int = 10_000,
    serve_uncertainty: float = 0.02,
    seed: int = 7,
) -> dict:
    rng = random.Random(seed)
    wins_a = 0
    sets_20 = 0
    sets_21 = 0
    sets_02 = 0
    sets_12 = 0
    total_games_acc: list[int] = []

    for _ in range(n):
        # Parameter uncertainty: sample serve probabilities each iteration.
        pa = min(0.95, max(0.35, rng.gauss(pa_serve, serve_uncertainty)))
        pb = min(0.95, max(0.35, rng.gauss(pb_serve, serve_uncertainty)))

        a_sets = 0
        b_sets = 0
        games = 0
        serves_first = rng.random() < 0.5

        while a_sets < 2 and b_sets < 2:
            a_won, g, serves_first = _play_set(pa, pb, rng, serves_first)
            games += g
            a_sets += int(a_won)
            b_sets += int(not a_won)

        total_games_acc.append(games)

        if a_sets == 2:
            wins_a += 1
            if b_sets == 0:
                sets_20 += 1
            else:
                sets_21 += 1
        else:
            if a_sets == 0:
                sets_02 += 1
            else:
                sets_12 += 1

    total_games_acc.sort()

    return {
        "p_win_a": wins_a / n,
        "p_win_b": 1 - wins_a / n,
        "set_scores": {
            "A_2_0": sets_20 / n,
            "A_2_1": sets_21 / n,
            "B_2_0": sets_02 / n,
            "B_2_1": sets_12 / n,
        },
        "total_games_mean": sum(total_games_acc) / n,
        "total_games_p10": total_games_acc[n // 10],
        "total_games_p90": total_games_acc[9 * n // 10],
        "simulation_count": n,
        "seed": seed,
    }


class TennisSimulationAgent(BaseAgent):
    agent_id = "T19"

    def analyze(self, payload: dict) -> AgentOutput:
        event_id = payload["event_id"]
        hb = payload.get("hold_break")
        if not hb:
            return self.insufficient(event_id, "T05 hold/break output missing")

        pa = payload.get("serve_point_prob_a")
        pb = payload.get("serve_point_prob_b")
        if pa is None or pb is None:
            return self.insufficient(event_id, "serve point probabilities missing")

        res = simulate_match(pa, pb)
        p = res["p_win_a"]

        return AgentOutput(
            event_id=event_id,
            agent_id=self.agent_id,
            model_version=self.model_version,
            status=Status.OK,
            primary_score=p,
            confidence=0.8,
            data_quality=payload.get("upstream_dq", 0.8),
            side_a_advantage=round(2 * (p - 0.5), 3),
            side_b_advantage=round(-2 * (p - 0.5), 3),
            uncertainty=round(abs(res["set_scores"]["A_2_1"] + res["set_scores"]["B_2_1"]) * 0.5, 3),
            key_reasons=[
                f"{res['simulation_count']} point-level sims: A {p:.1%}, "
                f"mean total games {res['total_games_mean']:.1f}"
            ],
            details=res,
        )
