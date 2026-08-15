import random

from sports_ai.evaluation.clv import clv_record, clv_summary
from sports_ai.evaluation.metrics import brier, calibration_slope, log_loss
from sports_ai.locking import canonical_hash, make_lock_row, verify_chain
from sports_ai.models.output import AgentOutput, Status


def test_log_loss_and_brier_perfect_vs_bad():
    assert log_loss([0.99, 0.01], [1, 0]) < log_loss([0.6, 0.4], [1, 0])
    assert brier([1.0, 0.0], [1, 0]) == 0.0


def test_calibration_slope_of_calibrated_data():
    rng = random.Random(0)
    probs, outcomes = [], []
    for _ in range(5000):
        p = rng.random()
        probs.append(p)
        outcomes.append(1 if rng.random() < p else 0)
    assert 0.85 < calibration_slope(probs, outcomes) < 1.15


def test_clv_summary_gate():
    recs = [clv_record(1.90, 1.75, 0.50, 0.55) for _ in range(350)]
    summary = clv_summary(recs)
    assert summary["gate_300_positive"] is True
    assert summary["mean"] > 0


def test_hash_chain():
    l1 = make_lock_row("evt_a", "r1", [{"x": 1}], prev_hash=None)
    l2 = make_lock_row("evt_b", "r1", [{"y": 2}], prev_hash=l1["payload_hash"])
    assert verify_chain([l1, l2])
    l2["prev_hash"] = "tampered"
    assert not verify_chain([l1, l2])


def test_canonical_hash_stable():
    assert canonical_hash({"b": 1, "a": 2}) == canonical_hash({"a": 2, "b": 1})


def test_insufficient_data_is_valid():
    out = AgentOutput.insufficient("evt_x", "T01", "1.0.0", "only 3 matches")
    assert out.status == Status.INSUFFICIENT_DATA
    assert out.primary_score is None
