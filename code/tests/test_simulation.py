from sports_ai.agents.tennis.simulation import simulate_match


def test_symmetric_players_near_even():
    res = simulate_match(0.64, 0.64, n=4000, seed=3)
    assert abs(res["p_win_a"] - 0.5) < 0.03


def test_better_server_favoured():
    res = simulate_match(0.68, 0.60, n=4000, seed=3)
    assert res["p_win_a"] > 0.6


def test_distributions_sum():
    res = simulate_match(0.66, 0.62, n=2000, seed=5)
    total = sum(res["set_scores"].values())
    assert abs(total - 1.0) < 1e-9
    assert abs(res["p_win_a"] + res["p_win_b"] - 1.0) < 1e-9


def test_total_games_sane():
    res = simulate_match(0.65, 0.63, n=2000, seed=9)
    assert 12 <= res["total_games_p10"] <= res["total_games_p90"] <= 39
