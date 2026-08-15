"""Football-specific research and pricing helpers."""

from .poisson_1x2 import (
    estimate_expected_goals,
    fit_poisson_1x2_from_history,
    poisson_1x2_probabilities,
)
from .poisson_1x2_v2 import (
    estimate_expected_goals_recency_weighted,
    fit_poisson_1x2_v2_from_history,
)
from .poisson_1x2_v3 import (
    estimate_expected_goals_opponent_adjusted,
    fit_poisson_1x2_v3_from_history,
)
from .poisson_1x2_v4 import (
    dixon_coles_1x2_probabilities,
    dixon_coles_score_matrix,
    dixon_coles_tau,
    fit_poisson_1x2_v4_from_history,
)
