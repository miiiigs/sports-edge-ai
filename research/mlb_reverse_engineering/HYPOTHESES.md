# MLB Reverse Engineering Hypotheses

Date created: 2026-08-13
Scope: research-only until broader scope is explicitly approved.

These hypotheses are not treated as true. They are explicit claims to test or reject.

## Status Vocabulary

- `PROPOSED`
- `TESTABLE`
- `BLOCKED_BY_DATA`
- `TESTING`
- `SUPPORTED`
- `REJECTED`
- `INCONCLUSIVE`

## HYP-001 Fair-line discrepancy

- `hypothesis_id`: `HYP-001`
- `source`: client voice-note plus general pricing thesis
- `claim`: A sufficiently large difference between an independent fair line and the available sportsbook line may identify value.
- `variables_required`: `model_fair_line`, `market_line`, `line_difference`, `entry_price`, `closing_line`, `result`
- `pre_event_cutoff`: model inputs and market snapshot must be frozen before the bet-time comparison
- `control_group`: picks with similar sports context but smaller or zero discrepancy
- `evaluation_metric`: CLV, calibration, out-of-sample EV, log loss/Brier where applicable
- `train_period`: UNKNOWN
- `validation_period`: UNKNOWN
- `test_period`: UNKNOWN
- `status`: `PROPOSED`
- `result`: `NOT_YET_TESTED`

## HYP-002 Line resistance

- `hypothesis_id`: `HYP-002`
- `source`: client voice-note
- `claim`: Heavy public or money concentration on one side combined with limited line movement may contain information.
- `variables_required`: `ticket_pct`, `money_pct`, `opening_line`, `current_line`, `time_to_event`, `market_consensus`, `sharp_book_line`, `news_events`
- `pre_event_cutoff`: all variables must reflect what was known before the claimed pick time
- `control_group`: similar games without concentrated money or without restricted movement
- `evaluation_metric`: incremental CLV and incremental predictive value after controlling for news
- `train_period`: UNKNOWN
- `validation_period`: UNKNOWN
- `test_period`: UNKNOWN
- `status`: `BLOCKED_BY_DATA`
- `result`: `RELIABLE_PUBLIC_MONEY_DATA_NOT_YET_CONFIRMED`
- `important_note`: No movement must not be interpreted automatically as proof that "the book knows the other side wins."

## HYP-003 Reverse line movement

- `hypothesis_id`: `HYP-003`
- `source`: client voice-note
- `claim`: Movement against the apparently popular side may provide incremental information.
- `variables_required`: `opening_line`, `pick_time_line`, `closing_line`, `ticket_pct`, `money_pct`, `injuries`, `lineups`, `weather`, `market_wide_news`, `time_to_event`, `liquidity_context`
- `pre_event_cutoff`: feature snapshots must be timestamped before the comparison point
- `control_group`: similar events without reverse movement
- `evaluation_metric`: CLV, out-of-sample return proxy, incremental information gain relative to baseline market features
- `train_period`: UNKNOWN
- `validation_period`: UNKNOWN
- `test_period`: UNKNOWN
- `status`: `BLOCKED_BY_DATA`
- `result`: `NEEDS_RELIABLE_MOVEMENT_AND_BETTING_SHARE_DATA`

## HYP-004 Cross-book disagreement

- `hypothesis_id`: `HYP-004`
- `source`: client voice-note plus sportsbook microstructure hypothesis
- `claim`: Some books may temporarily offer materially stale prices relative to market leaders or consensus.
- `variables_required`: `book_id`, `timestamp`, `entry_price`, `consensus_price`, `sharp_book_price`, `market_depth_proxy`, `closing_price`
- `pre_event_cutoff`: disagreement must be measured only from prices available at or before the decision time
- `control_group`: events where books are aligned within a normal spread
- `evaluation_metric`: CLV versus closing line and persistence of price divergence
- `train_period`: UNKNOWN
- `validation_period`: UNKNOWN
- `test_period`: UNKNOWN
- `status`: `PROPOSED`
- `result`: `NOT_YET_TESTED`

## HYP-005 Entry-to-close movement

- `hypothesis_id`: `HYP-005`
- `source`: client voice-note and bettor-timing claim
- `claim`: If historical selections consistently move favorably after release, the bettor may be identifying information before the broader market.
- `variables_required`: `entry_timestamp`, `entry_price`, `post_release_snapshots`, `closing_price`, `sports_context_changes`, `news_timestamps`
- `pre_event_cutoff`: the entry snapshot and subsequent market path must be reconstructable chronologically
- `control_group`: random market selections matched on sport, market type, and time-to-event
- `evaluation_metric`: mean CLV, positive-share, t-statistic, stability across unseen windows
- `train_period`: UNKNOWN
- `validation_period`: UNKNOWN
- `test_period`: UNKNOWN
- `status`: `PROPOSED`
- `result`: `NOT_YET_TESTED`

## HYP-006 Market selection

- `hypothesis_id`: `HYP-006`
- `source`: observed mix of ML, run line, and totals in the MLB sample
- `claim`: The choice between moneyline, run line, and totals may depend on the shape of the underlying outcome distribution rather than simple team preference.
- `variables_required`: `market_type`, `expected_runs_distribution`, `win_probability`, `run_distribution_shape`, `bullpen_state`, `pitching_matchup`, `park_factor`, `weather`, `price_by_market`
- `pre_event_cutoff`: distribution inputs must be point-in-time correct
- `control_group`: alternate market types on the same event
- `evaluation_metric`: market-level CLV and decision quality relative to market-specific baseline models
- `train_period`: UNKNOWN
- `validation_period`: UNKNOWN
- `test_period`: UNKNOWN
- `status`: `PROPOSED`
- `result`: `NOT_YET_TESTED`

## Current Scope Note

`OPEN_SCOPE_ISSUE: Is MLB research-only, or should MLB become a production sport?`

Current answer:

- unresolved;
- do not silently promote MLB into formal Pack V1 production scope.
