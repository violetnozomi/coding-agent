def test_persistent_process_benchmark_proves_core_contract(tmp_path) -> None:
    from nz_coder.evaluation.process_capability import (
        run_persistent_process_capability_benchmark,
    )

    result = run_persistent_process_capability_benchmark(tmp_path)

    assert [run["case_id"] for run in result["runs"]] == [
        "P1", "P2", "P3", "P4", "P5", "P6",
    ]
    assert all(run["process_handle_returned"] for run in result["runs"])
    assert all(run["can_read_after_return"] for run in result["runs"])
    assert all(run["can_reconnect"] for run in result["runs"])
    assert result["runs"][2]["can_write_after_return"] is True
    assert result["runs"][4]["exit_code"] == 7
    assert result["runs"][5]["process_count"] == 2
    assert result["structural_failures"] == 0
    assert result["orphan_process_count"] == 0
    assert "complete" in result["decision"]
