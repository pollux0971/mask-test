import numpy as np

from analysis.experiments.exp_vad_fusion import (
    N_ZONES,
    STRATEGIES,
    ONSET_MS,
    run_one_trial,
    sweep_coverage_and_amplitude,
    sweep_rigid_sensor_scenario,
)


def test_run_one_trial_returns_all_strategies():
    rng = np.random.default_rng(0)
    active_a = np.arange(8)
    active_b = np.arange(8)
    onsets = run_one_trial(rng, 0.5, 0.5, z_target=8.0, active_idx_a=active_a, active_idx_b=active_b)
    assert set(onsets.keys()) == set(STRATEGIES)


def test_strong_symmetric_signal_all_strategies_detect_near_ground_truth():
    """訊號夠強、兩邊對稱時，五種策略都該偵測到，且偏差在一個 frame（~33ms）內。"""
    rng = np.random.default_rng(1)
    active_a = np.arange(8)
    active_b = np.arange(8)
    onsets = run_one_trial(rng, 0.5, 0.5, z_target=10.0, active_idx_a=active_a, active_idx_b=active_b)
    for strat, onset_us in onsets.items():
        assert onset_us is not None, f"{strat} 沒有偵測到"
        assert abs(onset_us - ONSET_MS * 1000.0) < 40_000, f"{strat} 偏差超過一個 frame"


def test_blind_sensor_b_union_falls_back_to_a_without_cost():
    """B 完全沒反應時，union（OR 邏輯）該退回 A 自己的結果，不該因為 B 拖累。"""
    rng = np.random.default_rng(2)
    active_a = np.arange(8)
    active_b = np.array([], dtype=int)
    onsets = run_one_trial(rng, 0.5, 0.0, z_target=8.0, active_idx_a=active_a, active_idx_b=active_b)
    assert onsets["single_a"] is not None
    assert onsets["union_min"] == onsets["single_a"]


def test_blind_sensor_b_intersection_never_detects():
    """intersection（AND 邏輯）要求兩邊都偵測到；B 完全沒反應時必然偵測不到。"""
    rng = np.random.default_rng(3)
    active_a = np.arange(8)
    active_b = np.array([], dtype=int)
    onsets = run_one_trial(rng, 0.5, 0.0, z_target=8.0, active_idx_a=active_a, active_idx_b=active_b)
    assert onsets["intersection_max"] is None


def test_weak_signal_and_low_coverage_b_dilutes_merged_energy():
    """D22 的天花板效應陷阱：z_target 要夠弱才看得出稀釋效應。用主掃描裡
    已經展示過的組合（z_target=3.5, coverage_b=0.15）驗證 merged_energy
    確實比 single_a 明顯偏晚——不是重新斷言絕對數字，是釘住方向。"""
    rows = sweep_coverage_and_amplitude(
        coverage_a=0.5, coverage_b_values=(0.15,), z_target_values=(3.5,),
        n_geometries=2, n_noise_seeds=15,
    )
    row = rows[0]
    assert row["single_a_bias_ms_mean"] is not None
    assert row["merged_energy_bias_ms_mean"] is not None
    assert row["merged_energy_bias_ms_mean"] > row["single_a_bias_ms_mean"] + 10.0, (
        "稀釋效應沒有重現：merged_energy 應該明顯比 single_a 偏晚"
    )


def test_rigid_sensor_b_does_not_break_merged_energy_thanks_to_sigma_floor():
    """sensor B 完全沒動、baseline_sigma 貼著量化下限時，merged_energy 不該
    被拖垮——這是 `zone_energy()` 的 sigma_floor 保護在起作用，不是 Q3
    baseline 品質門控的功勞（quality_gated 在這個情境下等於 merged_energy）。"""
    rows = sweep_rigid_sensor_scenario(n_geometries=2, n_noise_seeds=15)
    for row in rows:
        assert row["merged_energy_detect_rate"] == 1.0
        assert abs(row["merged_energy_bias_ms_mean"]) < 40.0
        assert row["quality_gated_detect_rate"] == row["merged_energy_detect_rate"]
