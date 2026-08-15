# Meeting Alignment Update - 2026-08-13

Source reviewed:

- `meeting_notes/meeting_1`
- `meeting_notes/meeting_2.txt`
- `meeting_notes/meeting_3.txt`
- supporting assessment from `pasted-text.txt`
- official source verification for football data providers:
  - `https://www.football-data.co.uk/`
  - `https://www.api-football.com/pricing`
  - `https://www.football-data.org/`

## Summary

The meeting materially refines immediate execution priority.

The project should now be treated as:

- strategy-research and market-pricing first;
- small specialist-component prototype second;
- broader agent expansion only after evidence.

This is not a return to a generic "build all 50 agents in Pack order" workflow.

`meeting_2` adds concrete source and evaluation guidance on top of `meeting_1`.

`meeting_3` adds:

- a stronger low-cost validation mindset;
- sharper CLV-first discipline;
- a narrower "one-angle first" market-testing frame;
- and a practical note that most ideas should be rejected quickly if the graded numbers stay flat.

## What The Meeting Clearly Confirms

### 1. Project start is confirmed

The client states the project/business is starting now and requests an immediate five-day working period.

Record:

- `PROJECT_START = CONFIRMED_BY_CLIENT`
- `FIVE_DAY_INITIAL_WORK_PERIOD = REQUESTED`

### 2. MLB is the first reverse-engineering target

The client explicitly confirms starting with baseball and the 22 supplied picks.

The objective is to understand why those selections were made, not just reproduce the results.

Record:

- `MLB_STATUS = ACTIVE_STRATEGY_RESEARCH`

Do not yet record:

- `MLB_STATUS = PERMANENT_PRODUCTION_V1`

### 3. Football is an immediate parallel workstream

The client explicitly wants football/soccer information gathering started immediately because the English season is about to begin.

This should currently mean:

- collect fixtures and competition structure;
- identify data sources;
- begin team/home-away/context research;
- investigate useful markets such as BTTS and singles-first betting formats;
- prepare football research inputs.

This does not mean:

- rush all football Pack agents into production immediately.

### 4. Initial prototype should be small

The client repeatedly describes a smaller reasoning structure closer to:

- sports/context analysis;
- odds or oddsmaker analysis;
- betting-research/strategy analysis;
- final decision component.

This is more important for the immediate prototype than exact agent count.

### 5. Odds comparison is now urgent

The client explicitly asks about sources like Oddschecker and wants bookmaker comparison capability.

This creates an immediate market-data requirement for:

- bookmaker;
- event;
- market;
- selection;
- line;
- decimal odds;
- timestamp;
- opening/current/closing price;
- best price;
- consensus.

### 6. Football source path is now materially clearer

The new meeting note gives a practical football-source direction:

- `football-data.co.uk` for free historical CSV-based football research data;
- `API-Football` as the current leading live/current-season API candidate;
- `football-data.org` as a useful dev-friendly alternative, but not the main fit for the exact lower-cost historical betting-data need described in the meeting.

This is a meaningful reduction in research uncertainty for football.

### 7. Betting research should be a separate pipeline

The client wants research from:

- professional bettor material;
- books;
- YouTube videos;
- historical picks;
- Telegram groups.

This should be handled as a research pipeline that:

- extracts claims;
- converts them into hypotheses;
- versions them;
- passes them to historical testing.

It should not turn raw betting advice directly into production rules.

### 8. CLV is now even more central

The new meeting note strongly reinforces:

- compare your number to market price;
- judge performance on CLV, not superficial win rate;
- hunt weak markets and soft copies, not the most efficient flagship prices.

This matches the Developer Pack's anti-win-rate philosophy and strengthens the case for:

- immutable decision history;
- timestamped available price capture;
- closing-price evaluation;
- per-pick CLV tracking early in the project.

### 9. Early trial history matters

The client wants to start building history in a group relatively early.

That means paper/shadow outputs should begin early once a minimal decision loop exists, with immutable timestamps and no retrospective editing.

### 10. The first market experiment should be intentionally boring

`meeting_3` strongly supports starting with:

- one angle;
- one market family;
- one reference-book versus retail-book comparison loop;
- and strict grading against the close.

This is good guidance for early validation.

It should be interpreted as:

- narrow the experiment;
- not as "stop building the required architecture."

## What Must Still Be Treated Carefully

### 1. "Think like a casino" is not a literal rule

Translate this into:

- fair probability;
- implied probability;
- vig/devig;
- price discrepancy;
- line movement;
- cross-book disagreement;
- line resistance;
- stale pricing.

Do not encode:

- sportsbook manipulation claims as facts;
- narrative certainty as deterministic betting logic.

Translate the note's pricing discussion into:

- sharp-origin market hierarchy;
- retail-book shading;
- margin and favourite-longshot bias;
- steam vs isolated liability moves;
- weak-vs-strong market selection.

### 2. MLB scope is still only partly resolved

The meeting clearly makes MLB the first research target.

It does not clearly settle whether MLB becomes a permanent production sport after the reverse-engineering exercise.

### 3. Sharp-reference logic is useful, but not sufficient by itself

`meeting_3` leans heavily on the practical logic of:

- sharp reference price;
- retail mispricing;
- CLV grading.

That is useful.

But it must not be translated into:

- automatic belief that the sharp book is always correct;
- or a workflow that replaces the independent model with market copying.

### 4. Compensation terms are still not fully resolved

The meeting includes references to:

- approximately `$890` already sent;
- `$800` as a base layer;
- future increases if the group performs.

This is enough to record the client's statements, but not enough to replace prior compensation records with a final confirmed structure.

## Recommended Immediate Operating Sequence

Use this sequence:

`MLB reconstruction + football data reconnaissance -> strategy hypotheses -> data requirements -> minimum specialist modules -> early paper/shadow outputs -> narrow market experiment -> historical replay -> evidence-driven expansion`

Given `meeting_2`, football data reconnaissance should now begin with a real source plan, not just a vague future research task.

## Practical Initial Module Shape

Use a small provisional structure such as:

- `R01 Betting Research / Strategy Research`
- `A01 Sports Fundamentals / Context`
- `A02 Independent Pricing / Simulation`
- `M01 Market / Oddsmaker Intelligence`
- `D01 Final Decision / Gatekeeper`

These are responsibility buckets, not a claim that final naming or count is locked.

## Net Effect On Current Plan

The current plan should prioritize:

1. MLB reverse engineering;
2. football data/source reconnaissance in parallel, starting with `football-data.co.uk` and evaluating `API-Football` for live/current use;
3. odds-comparison data source investigation, especially sharp-reference versus retail comparison workflows;
4. Pack-faithful reconstruction of the starter code;
5. a small evidence-driven prototype loop;
6. a boring, measurable first market experiment rather than broad strategy sprawl.

The current plan should not prioritize:

1. mass agent implementation;
2. UI polish;
3. unsupported claims of early betting accuracy;
4. in-play or latency-sensitive engineering;
5. expensive market infrastructure before the simpler experiment is proven worthwhile.
