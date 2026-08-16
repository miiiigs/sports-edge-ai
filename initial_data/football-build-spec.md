# Football Build Specification

**Version 1.0 — 15 August 2026**
**Audience: the developer. Assumes strong software skills, no sports knowledge.**

> Standalone build. Separate repository, separate database, separate deployment from the tennis
> system. No shared code. If you find yourself writing a generic `SportModel` base class, stop —
> each sport has its own brain and the abstraction will cost more than it saves.

---

## 0. What you are building, and why it is different from the tennis system

Same core principle: estimate a probability, convert to a fair price, strip the bookmaker's
margin from the market price, compare, bet only on the gap.

But the football build has a stronger evidence base and a different shape.

**The evidence.** Edward Wheatcroft (London School of Economics) published a system called GAP
ratings — Generalised Attacking Performance — in the *International Journal of Forecasting* in
2020. It demonstrated **an average profit of around 0.8% per bet over twelve years**, tested
against a random-betting control, in the over/under 2.5 goals market across ten European
leagues. A 2021 follow-up extended the same framework to predicting match statistics directly,
and reported information beyond that reflected in the odds.

Nobody has published a replication failure. That makes it the best-evidenced thing in this
entire project.

**The counterintuitive bit, which the developer must not "fix".** GAP ratings work best using
**shots and corners as inputs, not goals.** Goals are the outcome you are trying to predict and
they are extremely noisy — a handful of events per match, heavily influenced by luck. Shots and
corners occur ten to thirty times per match and measure the *process* that generates goals. The
published profit figure is specifically for the shots-and-corners version. You will be tempted
to add goals as an input because it feels obviously relevant. Do not.

**The sober caveat.** Published market predictors decay. Across a large study of return
predictors, out-of-sample performance declined 26%, and post-publication performance declined
58% — roughly half attributable to data-mining bias and half to the market absorbing the
finding once it is public. Wheatcroft published in 2020. Expect meaningfully less than 0.8%
today. The edge is probably not dead, but it has been read.

---

## 1. Scope

### Leagues

| Country | Competitions |
|---|---|
| England | Premier League, Championship, League One, League Two |
| Spain | La Liga |
| Germany | Bundesliga |
| Italy | Serie A |
| France | Ligue 1 |
| Scotland | Premiership — **included with a wider threshold, see §7.4** |
| Europe | Champions League, Europa League |
| England (cups) | FA Cup, EFL Cup — **restricted markets only, see §7.5** |

**Explicitly excluded:** National League and below (no reliable public shots/corners/odds data —
GAP ratings would run blind), Portugal (thin data, wide margins, untested in the literature),
all women's football, all friendlies.

### Markets

| Market | Priority | Notes |
|---|---|---|
| Over/Under 2.5 goals | **Primary** | This is the market the published profit figure applies to |
| Both Teams To Score | Primary | Separate published evidence base, see §5.3 |
| Total corners | Secondary | GAP predicts corners natively; bookmaker margins are wider — see §7.6 |
| Asian Handicap | Phase 2 | See §5.4 for the line-type finding |
| 1X2 (match result) | **Deprioritised** | Most efficient market, most attention, hardest to beat |

Note the deliberate absence of 1X2 from the priorities. It is the market everyone models and the
one bookmakers price most sharply. Nobody forecasts draws well — published skill scores sit at
1.4–3.0% with discrimination around 0.0–0.1% — because the draw price sits near the base rate,
which is the best anyone can do.

---

## 2. Data sources

### 2.1 Football-Data.co.uk (free, no key) — the backbone

`https://www.football-data.co.uk/data.php`

CSV per league per season. Field definitions at `https://www.football-data.co.uk/notes.txt` —
read that file before writing the parser.

Columns you need:

| Field | Meaning |
|---|---|
| `Date`, `HomeTeam`, `AwayTeam` | Fixture identity |
| `FTHG`, `FTAG`, `FTR` | Full-time home goals, away goals, result |
| `HS`, `AS` | **Shots** — home, away |
| `HST`, `AST` | Shots on target |
| `HC`, `AC` | **Corners** — home, away |
| `HY`, `AY`, `HR`, `AR` | Yellow and red cards |
| `Referee` | Referee name — needed for the cards model later |
| `B365H`, `PSH`, `WHH` … | Opening and closing odds from ~20 bookmakers |
| `BbAv`, `BbMx` / `Avg`, `Max` | Average and maximum odds across the market |

This single source gives you results, the GAP inputs, and both opening and closing odds for a
dozen-plus books, going back two decades. It is free and it is the most valuable asset in the
build.

**Coverage caution:** shots and corners are present for the English divisions down to League Two
and for the big five leagues. Coverage for Scotland and for older seasons is patchier. Assert on
null rates per league per season at ingest and refuse to rate a league where the null rate
exceeds 5%.

### 2.2 FBref / StatsBomb (free, scraping)

`https://fbref.com`

Deeper match statistics including expected goals (xG) for the top five leagues. Used as a
**sanity check** on GAP, not as a model input (see §6). Respect their rate limits — one request
every few seconds, cache aggressively.

### 2.3 Understat (free)

`https://understat.com` — xG for the big five leagues, easily parsed. Alternative to FBref for
the xG check.

### 2.4 Live odds feed (paid, API key)

Same options as the tennis build:

- **The Odds API** — start here, cheap, free dev tier.
- **OddsJam** or equivalent — more books, faster, includes sharp books.
- **Betfair Exchange API** — free, requires funded account and app key.

You need a **sharp reference price**. Pinnacle is the benchmark but does not serve UK customers
directly — obtain their prices via a provider that carries them, or use the Betfair Exchange as
the sharp reference. **Settle this before writing the pricing module.** The de-vig layer, the
edge calculation and the entire CLV measurement system all depend on having a credible sharp
line. Substituting a soft book here silently invalidates every number the system produces.

### 2.5 Live fixtures, lineups and team news (paid)

- **API-Football** (`api-football.com`) or **Sportmonks** — fixtures, lineups, injuries,
  suspensions. Needed by the veto agent.
- Confirmed lineups publish approximately one hour before kickoff. This is the single most
  valuable free-ish piece of late information in football.

---

## 3. Database

Postgres. Same discipline as the tennis build.

```
teams              (team_id, canonical_name, aliases[], league_id, country)
fixtures           (fixture_id, date, kickoff_utc, competition_id, season,
                    home_team_id, away_team_id, status)
results            (fixture_id, fthg, ftag, hs, aws, hst, ast, hc, ac,
                    hy, ay, hr, ar, referee, xg_home, xg_away)
gap_ratings        (team_id, as_of_date, competition_id, statistic, venue,
                    attack_rating, defence_rating, n_matches, model_version)
odds_snapshots     (snapshot_id, fixture_id, book, market, line, selection,
                    decimal_odds, captured_at, is_closing)
predictions        (prediction_id, fixture_id, model_version, market, line,
                    predicted_stat, p_outcome, fair_odds, generated_at)
decisions          (decision_id, fixture_id, model_version, market, line, decision,
                    model_p, fair_odds, sharp_odds, devig_method, devig_fair_odds,
                    best_book, best_odds, edge_pct, min_acceptable_odds,
                    stake_pct, veto_reason, generated_at, published_at)
settlements        (decision_id, outcome, closing_odds, closing_margin,
                    clv_pct, pnl_units, settled_at)
data_health        (source, last_successful_pull, rows_ingested, status, checked_at)
```

Same three rules as tennis:

1. **Log NO_BET decisions.** Without them you cannot tell whether the system is too tight or too
   loose, and you cannot audit why a fixture was skipped.
2. **Version everything together** — model code, feature code and calibration as one bundle,
   stamped on every prediction and decision.
3. **`odds_snapshots` is append-only with timestamps.** Never overwrite a price.

**Team-name matching** is the equivalent headache to player names in tennis. Football-Data uses
"Man United", the odds API may use "Manchester United", FBref uses "Manchester Utd". Build a
canonical team table with aliases, resolve once, and **assert loudly on unmatched names** rather
than dropping fixtures. Silently dropped fixtures bias a backtest invisibly.

---

## 4. GAP Ratings — the core engine

### 4.1 The concept

Every team carries an **attacking rating** and a **defensive rating** for each tracked statistic.

- The attacking rating estimates how much of that statistic the team generates against an
  average opponent.
- The defensive rating estimates how much of it the team concedes to an average opponent.

Ratings are maintained **separately for home and away**, because home advantage is large and
team-specific.

Ratings are also maintained **separately per statistic**. If you track shots and corners, that
is four numbers per team per venue: shots-attack, shots-defence, corners-attack,
corners-defence — times two for home and away.

### 4.2 Prediction

For a fixture between home team `i` and away team `j`, for statistic `s`:

```
Expected_home_s = ( A_home[i][s] + D_away[j][s] ) / 2
Expected_away_s = ( A_away[j][s] + D_home[i][s] ) / 2
```

Read plainly: *what team i usually generates at home, averaged with what team j usually concedes
away.* That is the whole prediction step.

### 4.3 Update

After the match, with `O` the observed value of the statistic:

```
A_home[i][s] += λ × ( O_home_s − Expected_home_s )
D_away[j][s] += λ × ( O_home_s − Expected_home_s )
A_away[j][s] += λ × ( O_away_s − Expected_away_s )
D_home[i][s] += λ × ( O_away_s − Expected_away_s )
```

`λ` is the learning rate — how much a single match moves a rating. Start at **0.05**. Wheatcroft
tunes this; you may tune it too, **but only on a walk-forward basis and you must record every
value you tried** (see §9.5). A learning rate optimised on the full dataset is the single most
common way to produce a beautiful backtest and a losing live model.

### 4.4 Initialisation and new teams

- Initialise every rating to the league-and-venue average for that statistic.
- A promoted or relegated team carries zero current-division observations and a prior built from
  a different opponent distribution. **The market has exactly the same problem**, so this is not
  automatically an edge — it is a shared increase in variance. Realistically, quantitative
  modelling adds nothing over market prices for these teams until roughly 8–10 matches into the
  season.
- **Rule: no bets involving a newly promoted or relegated team until they have 8 league matches
  in the current division.** Encode this as a veto condition.

### 4.5 From predicted statistics to probabilities

GAP gives you expected shots and expected corners. You now need `P(over 2.5 goals)`,
`P(both teams score)`, `P(over 9.5 corners)`.

Two routes, build both:

**Route A — direct classification (build first).** Fit a logistic regression (or gradient-boosted
trees) with the GAP-predicted statistics as features and the binary market outcome as the
target. Features: predicted home shots, predicted away shots, predicted home corners, predicted
away corners, their sum, their difference, plus league and venue indicators. Target: `over 2.5`
= 1/0, `BTTS` = 1/0.

This is simple, robust, and directly optimises the thing you care about.

**Route B — goal-expectation model (build second).** Convert predicted shots into expected goals
per side, then use a bivariate Poisson with the Dixon–Coles low-score correction to build a full
scoreline matrix. From the matrix:

- `1X2` = summing triangles of the matrix
- `Correct score` = a single cell
- `Over/Under N` = cells where `i + j > N`
- `Asian handicap` = cells grouped by `i − j`

The Dixon–Coles correction matters: independent Poisson systematically under-predicts 0-0, 1-0,
0-1 and 1-1, and the correction (`ρ ≈ −0.1285` on typical EPL data) fixes exactly those four
scorelines and **raises the draw probability.** Any naive Poisson model systematically
under-prices the draw. Home advantage enters as `γ ≈ 0.2945`, a roughly 1.34× multiplier on home
expected goals.

Route B is more work but it prices every market from one object, which means a mispricing in one
market implies a mispricing in the others — and it lets you cross-check Asian handicap against
1X2 for internal consistency.

**Do not bother with time-decay weighting.** The optimal decay parameter within a single season
is zero, and across five seasons it buys a log-likelihood improvement of 0.23. Recency weighting
is not where the money is.

**Baseline honesty:** a Dixon–Coles model on public data is a baseline, not an edge. The
practitioner who most carefully replicated it put it plainly: *these models will not make you
money on their own.* The edge, if there is one, comes from GAP's process-based inputs plus the
price-scanning layer — not from the scoreline matrix.

---

## 5. Supporting evidence for specific markets

### 5.1 Over/Under 2.5 — the primary market

Wheatcroft (2020), *International Journal of Forecasting* 36(3), 916–932. Free accepted version
at the LSE research repository. This is the paper that defines GAP ratings and reports the 0.8%
figure. **Have the developer read it.** The equations are in §2 of the paper.

A worked Python implementation walkthrough exists publicly (search "Implementing the GAP ratings
in python"). Use it as a reference for structure, not as a dependency.

### 5.2 Predicting match statistics — the extension

Wheatcroft (2021), *Journal of Sports Analytics*. Free on arXiv as 2001.09097. Establishes that
GAP ratings predict match statistics accurately enough in advance to be informative beyond the
odds, and introduces Bivariate Poisson ratings as an alternative. This is the paper that
justifies pricing corners and shots markets from the same engine.

### 5.3 Both Teams To Score

da Costa, Marinho & Pires (2022), *International Journal of Forecasting* 38(3), 895–909. Six
seasons (2013–2019), nine leagues, odds from 19 bookmakers, machine-learning classifiers. Finds
the BTTS market can be beaten in identifiable scenarios. This is a separate, independent
evidence base from GAP — which is why BTTS is a primary market rather than an afterthought.

### 5.4 Asian Handicap — a structural finding worth having

Whelan et al., across 84,230 and 67,373 matches, measured loss rates by handicap line type:

| Line type | Loss rate |
|---|---|
| Integer (0, −1, +1) | **3.24%** |
| Quarter (.75) | 3.57% |
| Quarter (.25) | 3.61% |
| Half (.5) | **4.16%** |

Integer and .75 lines are structurally cheaper than .5 lines. The same work found **no
favourite-longshot bias in Asian handicap markets**, unlike 1X2.

This is a free 0.9 percentage points, available before any modelling. When the system reaches
Asian handicap in Phase 2, **prefer integer and .75 lines** and encode the preference explicitly.

Constantinou (2022), *Investigating the efficiency of the Asian handicap football betting market
with ratings and Bayesian networks* (arXiv 2003.09384) is the reference for threshold selection
— it tested all possible betting decision thresholds between predicted and published odds across
13 EPL seasons, comparing average against best-available odds and optimising for both ROI and
profit. Read it before finalising §7.4.

### 5.5 What the sceptics found

Winkelmann, Ötting, Deutscher & Makarewicz (2024), across 14 seasons of the big five leagues:
inefficiencies are short-lived and do not persist across time or leagues. Their Monte Carlo work
shows that in a **fully efficient** market there is a **77.63% probability** of observing at
least one season with a statistically significant home bias across 14 seasons.

Search enough seasons, leagues and bias types and you will find "something" four times out of
five when nothing is there. This is why §9.5's configuration counting is not bureaucracy.

---

## 6. The xG sanity check

Expected goals (xG) is a per-shot model of chance quality. Opta's version uses more than 20
variables per shot trained on around a million shots.

**Use it as a check, not an input.**

Rule: if GAP's predicted attacking output for a team diverges sharply from their recent xG
trend — say, GAP has them generating well above league average while their xG per match sits
well below — **flag the fixture and skip it.** That divergence usually means one of the two
inputs is being distorted by something the model has not seen (a tactical change, a key
absence, a run of games against unusually open opponents).

**Why not use xG directly as a model input?** It measurably beats raw goals as a forward
predictor (Pearson r with future goals of 0.71 versus 0.58; next-match result accuracy 59.2%
versus roughly 50–52%). But the gap is material rather than enormous, and the market knows this
too. More importantly, the popular xG-divergence edge — "this team is underperforming their xG,
they are due to revert" — is more fragile than its proponents admit, because **the market
regresses it as well.** Finishing skill is real but nearly undetectable at bettable sample
sizes: a player genuinely 25% better than average, taking 100 shots a season, has only around a
70% probability of out-performing his cumulative xG in four seasons out of five. Empirical-Bayes
work finds essentially zero year-on-year correlation in shrunk goals-per-xG.

Working prior: treat finishing over- and under-performance as noise. Use xG to catch errors, not
to generate opinions.

**Coverage note:** public xG is not available for League One and League Two, and public xG
quality for those divisions is materially worse where it exists. The xG check therefore runs on
the top five leagues and the Premier League/Championship only. Lower divisions run without it —
which is another reason their threshold is wider.

---

## 7. The Manager

### 7.1 Structure

Football has one primary model, not three agents. The consensus layer here is between **GAP
Route A (direct classification)** and **GAP Route B (goal-expectation matrix)** once both exist.
Until Route B is built, Route A runs alone with a wider threshold.

| Configuration | Requirement |
|---|---|
| Route A only | Threshold +1 percentage point over the table in §7.4 |
| Route A and Route B | Both must agree on direction; probability spread ≤ 0.06 |
| xG check failed | NO BET regardless |

### 7.2 De-vigging

Identical to the tennis build. Take the sharp reference price for the market, compute
`π_i = 1/o_i`, `S = Σπ_i`, and solve the power method for `k` such that `Σ(π_i)^k = 1`.

For three-way markets (1X2) the same method applies across all three outcomes.

**Do not use naive proportional de-vig.** For balanced two-way markets like over/under it barely
matters; for anything involving a genuine longshot it matters enormously and it manufactures
false value exactly where the bookmaker's margin is concentrated. Record the method used on
every decision row.

**And note this**, because it changes how you think about the margin: the headline overround
understates what the margin actually costs. Across 151,683 football matches, overround-predicted
loss rates of 7.1% ran against **actual** loss rates of **8.7%**. Broken down by odds decile,
the shortest prices cost around 3% and the longest around 19%. Bookmakers load margin onto
longshots. The headline margin is a floor on cost, not an estimate of it.

### 7.3 Edge and the price scanner

```
edge_pct = (model_p × best_available_odds_across_all_books) − 1
```

**The sharp price establishes what fair is. The best soft price is what we bet into.** The gap
between those two is where the most reliably measured money in this entire field lives.

The measured result: taking the best soft-book price where it exceeds a sharp book's de-vigged
fair odds returned **+3.40% yield over 22,281 bets**, rising to **+13.22%** where the edge
exceeded 5% (n = 1,927). The control — deliberately betting *below* the sharp fair price —
returned **−3.08% over 44,673 bets.**

Note the scale. That is roughly four times the GAP model's own published edge. **The number of
books you can read matters more than the sophistication of the model.** Prioritise breadth of
odds coverage over model refinements.

Bookmaker margins on 1X2 for context: Pinnacle around 2.7%, bet365 around 5.5%, Ladbrokes around
7.8%. A five-point margin spread against a sub-one-percent modelling edge tells you where the
engineering effort belongs.

Store `min_acceptable_odds` on every BET — the price at which the edge falls to threshold. If
the price drifts below it before publication, the pick is dead and must not go out.

### 7.4 Thresholds

| Segment | Minimum edge after de-vig |
|---|---|
| Premier League, La Liga, Bundesliga, Serie A, Ligue 1 | **5%** |
| Championship | 6% |
| League One, League Two | **7%** |
| Champions League, Europa League | 5% |
| Scotland | **8%** |
| Domestic cups (restricted markets only) | **8%** |
| Route A running alone | add 1 point to all of the above |

**Why lower leagues need more, not less.** The folklore says lower leagues are soft and therefore
easier. The margin data partly contradicts it: Premier League margins run about 2.5–4%, softer
leagues about 6–8.5%. You face a softer market **and** two to three times the margin **and**
lower stake limits **and** worse data. To profit in League Two you need an edge roughly double
the one you would need in the Premier League. There is a real case for lower leagues — less
analyst attention, slower reaction to team news — but "less market attention" alone is not
sufficient, and it is routinely presented as if it were. **A soft market means a wider spread
AND a worse market estimate. You have to beat both.**

**Why Scotland is taxed rather than cut.** The objection to Scotland is not that two clubs
dominate — corners and BTTS markets do not care much who wins. The objection is the margin: 6–8%
against 2.5–4% in England, on markets where our edge is under one percent. Same bet, worse
price. So it stays in the model with a threshold that reflects the price we are paying, rather
than being cut for a reason that does not apply to the markets we actually bet.

### 7.5 Cup competitions — restricted markets

Domestic cup ties are the hardest fixtures in football to price. Squads rotate heavily, you do
not know who is playing until an hour before, and GAP ratings built on league form may describe
a side that does not take the field.

**But rotation matters far less for corners and BTTS than for the match result.** A rotated side
still attacks, still concedes, still wins corners. That is the reason cups stay in.

**Hard constraint, enforced in code:**

| Competition type | Permitted markets |
|---|---|
| FA Cup, EFL Cup | Corners, BTTS **only** |
| League fixtures | All markets |
| Champions League, Europa League | All markets |

No match result, no Asian handicap, no over/under goals in domestic cups. Additionally: skip
any tie involving a team with no GAP rating in the system (non-league opponents in early FA Cup
rounds).

### 7.6 Corners — the margin trap

Corners markets are genuinely less well priced. Bookmakers themselves treat them as lower
confidence, and GAP predicts corners natively — it is literally what the ratings were built for.

**But the margins are much wider.** A published comparison of one Premier League fixture across
a dozen bookmakers on an over/under 10.5 corners line found the tightest margin just above 7.5%,
with several rivals above 10%. Against a sub-one-percent modelling edge, that is fatal.

**Rule: corners bets only at the tightest-margin book available for that market.** Compute the
booksum for the corners market at each book, rank them, and only bet where the margin is below a
configured ceiling (start at 8%). The margin spread across books on corners is larger than the
edge itself, so book selection is doing more work here than the model is.

### 7.7 Cards — Phase 3, with a warning

Cards markets are thin and slow-moving, which makes them attractive. The referee is one of the
few genuinely predictive inputs, and Football-Data carries referee names, so a referee database
is cheap to build.

**One critical constraint:** VAR moved card rates substantially — a difference-in-differences
study using the Championship as a control across 1,864 matches found penalties up 0.07 and reds
up 0.07, but **yellows up 0.37 per match**, roughly 12% inflation on the yellow line. Because
the Championship was the control, **you cannot transfer a card model between the Premier League
and the Championship.** Fit separately per competition, always.

Build the referee data collection now as a passive background job — it costs nothing and the
data accumulates. Do not build the model in the first 30 days.

---

## 8. The Veto Agent

Kills bets, never creates them. Same architectural constraint as tennis, same reasoning: general
football knowledge is already in the sharp price; only late, fast-moving or non-public
information is not.

| Condition | Action |
|---|---|
| Confirmed lineup missing 2+ expected starters | VETO |
| Key attacking player ruled out within 24 hours of kickoff | VETO |
| Newly promoted/relegated team with fewer than 8 matches in the division | VETO |
| Manager change within the last 3 matches | VETO |
| Team played a European fixture within 4 days (congestion flag) | FLAG, widen threshold by 1 point |
| Cup tie with any unrated opponent | VETO |
| xG divergence check failed (§6) | VETO |
| Odds moved more than 15% against our selection since generation | VETO |
| Any data source stale (§9.1) | VETO all picks in the run |
| Match is a dead rubber (both teams' season decided) | VETO |

**On the congestion flag — be honest about what it is.** Fixture congestion does not reliably do
what pundits claim. Two matches within four days produced a negative attacking effect in the
Bundesliga that **did not replicate** in Spain or England, and defensive strength actually
*improved* for home games under congestion across all three leagues, with substantial team and
season heterogeneity. So the flag widens the threshold — it does not generate a directional
view. Any tip whose load-bearing argument is "they played Thursday" is asserting an effect the
literature does not cleanly support.

Log every veto with its reason and settle vetoed picks on paper anyway. That is the only way to
find out whether the veto agent is earning its keep or just costing bets.

---

## 9. Safeguards

Identical in structure to the tennis build. Build them in the first two weeks.

### 9.1 Data freshness gate

| Source | Maximum staleness |
|---|---|
| Odds feed | 15 minutes |
| Football-Data results | 48 hours |
| GAP ratings rebuild | 24 hours |
| Fixture and lineup feed | 2 hours on matchday |

Fail the run and alert. Never publish off stale data.

### 9.2 Calibration tracking

Bin every forecast into deciles. When the model says 60%, does it happen 60% of the time? Plot a
reliability diagram weekly, report Brier score.

**This matters more than CLV in the first month.** If the model is poorly calibrated, every edge
calculation downstream is wrong — you are comparing a bad probability to a good price and calling
the difference value. It looks fine on paper and it bleeds money. And calibration accumulates
across *all* forecasts, including no-bets, so it reaches a usable sample far faster than P&L
does.

Recalibrate with Platt scaling or isotonic regression on a held-out period if systematic bias
appears, and version the calibration with the model.

### 9.3 CLV logging

```
clv = bet_odds / closing_odds / (1 + closing_margin) − 1
```

Divide by the closing margin. Beating the close by less than the closing margin is not an edge.

Capture the closing price for every fixture on the slate, bet or no-bet. The relationship
between predicted yield from the bet-to-closing odds ratio and realised yield has been measured
across 87,960 odds pairs with a slope of almost exactly 1.00 — the de-vigged closing price
behaves as an unbiased estimate of true probability, which is why this is the benchmark.

### 9.4 Alerts and stops

- Rolling 14-day mean CLV negative → email immediately.
- Weekly bet volume more than 50% off the trailing 8-week mean → alert (usually a data bug).
- Any calibration decile more than 10 points off over 100+ forecasts → alert.
- **Drawdown stop at 20% of notional bankroll → everything pauses for review.** A working model
  with 6,562 picks and a genuine 3.79% yield still produced 1,000-bet windows that looked like
  1-in-40 failures. The stop is not a diagnosis, it is a circuit breaker against continuing on
  hope.

### 9.5 Validation discipline

- **Walk-forward only.** Train on seasons 1–N, test on N+1, roll forward. Never fit and test on
  the same period.
- **Use opening odds for the strategy, closing odds only for validation.** Opening prices are
  when soft books are actually beatable, before they have seen the money. Using closing odds in
  the strategy itself is look-ahead bias.
- **Save every feed with an as-of timestamp.** This is the mechanical defence against look-ahead
  bias, and it is the single most common way a backtest lies.
- **Minimum 300–500 bets** across multiple seasons before any backtest result means anything.
- **Count and record every configuration tried.** Testing just 10 strategy variants yields an
  expected in-sample Sharpe ratio of 1.57 with **zero true skill**. A backtest reported without
  the configuration count is not evidence of anything.
- **Cross-validate rather than single train/test split.** Models using basic train-test splits
  showed roughly 12% higher backtest performance and then dropped about 28% in forward testing
  relative to cross-validated equivalents.
- **Paper-trading mode from day one**, same code path as live, one flag.

### 9.6 Staking

```
f* = (p × o − 1) / (o − 1)
stake = min(0.5 × f*, 0.02)
```

Half Kelly, capped at 2%. Reasoning identical to the tennis build: full Kelly is optimal only if
`p` is known, ours is estimated, and threefold overestimation makes you more likely to lose than
profit *even with a genuine edge*.

**No accumulators.** Five legs at a 5% hold is a 22.6% hold; ten legs is 40.1%.

**Check every multi-selection card for shared latent factors.** Three "different" bets that all
depend on the same team scoring are one bet in three costumes, and the correlation is invisible
in the staking arithmetic unless you look for it.

---

## 10. Scheduling

All times UK.

| Time | Job |
|---|---|
| **Monday 09:00** | Full week's slate generated. Ratings rebuilt from weekend results. **Not published** — this is an internal early read on where value may sit. |
| **Wednesday 09:00** | Midweek fixtures (cups, European nights, EFL midweek rounds) regenerated and published at 09:00 on matchday. |
| **Friday 09:00** | Weekend slate regenerated with current team news. |
| **Matchday 08:00** | Final run. Veto agent sweeps injuries and team news. **Publish.** Every pick carries its minimum acceptable price. |
| **Kickoff − 75 min** | Lineup check. Publish retractions if the veto conditions fire. |
| **Kickoff** | Capture closing price for every fixture on the slate, bet or no-bet. |
| **Sunday 23:00** | Weekly reporting: CLV, calibration, volume, drawdown. |

**Why 08:00 on matchday rather than publishing Monday.** Publishing early gets you the best
prices, but you are blind to team news and the price you flagged may be gone by kickoff.
Publishing at 08:00 gives followers the whole day and gives the veto agent the overnight news
cycle. The lineup check at 75 minutes is the last line of defence.

### The fixed slate rule

**Every fixture in every in-scope competition goes onto the slate. Every matchday. No
exceptions.**

The model does not choose which matches to examine. If it did, the selection step would be doing
work you cannot audit, and you could never distinguish the model's contribution from the effect
of which fixtures it happened to look at.

**Zero-pick days are a correct output.** The system must be able to say "nothing today" cleanly.

---

## 11. What gets recorded with every pick

Same eight fields as the tennis build:

1. Sporting assessment.
2. Estimated probability **with an interval**.
3. Fair price implied by it.
4. Market price, book, and named de-vig method.
5. Edge as a number.
6. **Named mechanism** for why this number should beat the closing line — a market segment, a
   market type, or an information type the closing price does not contain. No mechanism, no bet.
7. What would make us wrong.
8. BET or NO BET, with stake as a function of edge.

**Banned as primary arguments:** "scored in their last five", "kept four clean sheets", "unbeaten
in six", league position, head-to-head, "due a win". These are the most easily computed
statistics in football, every bookmaker's model contains them, and they are fully in the price
before anyone has looked. They are also backward-looking outcome measures — which is precisely
what GAP ratings replace with process measures. They are not missing from the system; they have
been superseded by something better.

**Banned language:** banker, lock, guaranteed, certainty, free money, can't lose, max play.

---

## 12. Honest statement of limitations

1. **The published edge is 0.8% per bet.** That is real over 68,672 bets across twelve years, and
   it is thin. On £100,000 staked it is £800. Over any hundred bets it is invisible — the swing
   at those odds is thousands in either direction and tells you nothing.

2. **Publication decay is real and quantified.** Published predictors decline roughly 26%
   out-of-sample and roughly 58% post-publication. Wheatcroft published in 2020. Budget for
   meaningfully less than 0.8%.

3. **Most apparent inefficiencies are artefacts.** In a fully efficient market there is a 77.63%
   chance of finding at least one "significant" bias season across 14 seasons. Finding something
   is the default, not the signal.

4. **The price-scanning layer probably contributes more than the model.** Margin arbitrage
   against a sharp de-vigged price is measured at +3.40% over 22,281 bets. That is roughly four
   times the model's own edge. Engineering effort should be allocated accordingly.

5. **The market beats the model on the headline market.** Bookmaker odds beat Poisson regression
   on every 1X2 outcome in published decomposition work. This is why 1X2 is deprioritised and
   over/under, BTTS and corners are the targets.

6. **Fade-the-public does not work.** Using actual volume data on 4,119 matches, roughly 80% of
   money sat on the over with **no exploitable price distortion** (mean return difference 0.018,
   p > 0.05). Competition between bookmakers closes it, not benevolence. Knowing what the public
   bets is not knowing where the price is wrong.

7. **Capacity binds before edge does.** UKGC 2024: 4.31% of accounts commercially restricted, and
   46.78% of restricted customers were in profit against 25.42% of all customers. Around 60% of
   stake-factored accounts were capped at 10% or less of their desired stake.

---

## 13. References

- **Wheatcroft, E. (2020)** — *A profitable model for predicting the over/under market in
  football*. International Journal of Forecasting 36(3), 916–932. Free accepted version at the
  LSE research repository (`researchonline.lse.ac.uk`). **The core paper. Read it first.**
- **Wheatcroft, E. (2021)** — *Forecasting football matches by predicting match statistics*.
  Journal of Sports Analytics. Free on arXiv: `arxiv.org/abs/2001.09097`
- **da Costa, I., Marinho, L. & Pires, C. (2022)** — BTTS market study. International Journal of
  Forecasting 38(3), 895–909.
- **Constantinou, A. (2022)** — *Investigating the efficiency of the Asian handicap football
  betting market with ratings and Bayesian networks*. arXiv:2003.09384. Threshold selection
  methodology.
- **Constantinou, A. & Fenton, N. (2013)** — pi-ratings. Alternative rating system worth
  benchmarking GAP against.
- **Dixon, M. & Coles, S. (1997)** — *Modelling association football scores and inefficiencies in
  the football betting market*. The low-score correction.
- **Whelan, K. et al.** — Asian handicap line-type loss rates. Free PDF at
  `karlwhelan.com/Papers/RBF.pdf`
- **Winkelmann, D., Ötting, M., Deutscher, C. & Makarewicz, T. (2024)** — *Are Betting Markets
  Inefficient? Evidence From Simulations and Real Data*. Journal of Sports Economics. The
  sceptical case and the Monte Carlo false-positive result.
- **Angelini, G. & De Angelis, L.** — efficiency of online football betting markets using
  best-available odds across bookmakers.
- **Buchdahl, J.** — *The Wisdom of the Crowd* and the margin-arbitrage results. Free PDF at
  `football-data.co.uk/The_Wisdom_of_the_Crowd_updated.pdf`
- **Football-Data.co.uk field notes** — `football-data.co.uk/notes.txt`
- **Wunderlich et al.** — empty-stadium home advantage study.
