import os
import json
import tempfile
import hashlib
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from codemonkeys.swe_bench.swe_bench_verified import SWEBenchProblem
from codemonkeys.swe_bench.test_runner.test_execution import (
    run_swebench_test,
    SWEBenchTestResult,
)
from codemonkeys.trajectory_data.structs import RegressionTestExecutionResults, PatchInfo
from codemonkeys.utils.concurrency import parallelize
import pydra
from codemonkeys.utils.shared_config import CodeMonkeysConfig, ConfigWithContainerExec

from agentless.test.run_tests import run_tests, NOOP_PATCH
from swebench.harness.grading import get_logs_eval
from agentless.test.select_regression_tests import select_tests
import argparse


class RegressionTestConfig(CodeMonkeysConfig, ConfigWithContainerExec):
    def __init__(self):
        super().__init__()

        self.num_problems_in_parallel = 32
        self.num_threads_per_problem = 1


def get_chosen_tests(problem: SWEBenchProblem) -> dict[str, str]:
    passing_tests = get_passing_tests(problem)
    passing_tests_dict = {
        "instance_id": problem.instance_id,
        "tests_passing_in_original_repo": passing_tests,
    }

    output_file = f"chosen_tests_{problem.instance_id}.json"

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    ) as temp_file:
        with open(temp_file.name, "w") as f:
            json.dump(passing_tests_dict, f)

        args = argparse.Namespace(
            output_folder="regression_test" + problem.instance_id,
            passing_tests=temp_file.name,
            dataset="princeton-nlp/SWE-bench_Verified",
            instance_ids=[problem.instance_id],
            output_file=output_file,
            model="gpt-4o",
            backend="openai",
            target_id=None,
            mock=False,
        )

        try:
            select_tests(args)
            with open(output_file, "r") as f:
                chosen_tests = json.load(f)
        finally:
            # Clean up the temporary file
            import os

            if os.path.exists(temp_file.name):
                os.unlink(temp_file.name)

    return chosen_tests


def get_passing_tests(
    problem: SWEBenchProblem,
    patch: str | None = None,
    chosen_tests: dict[str, str] | None = None,
) -> list[str]:
    patch_hash = hashlib.md5((patch if patch else "empty_patch").encode()).hexdigest()
    run_id = f"{problem.instance_id}_{patch_hash}"
    regression_test_file = ""
    if chosen_tests is not None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as temp_file:
            json.dump(chosen_tests, temp_file)
            regression_test_file = temp_file.name
    input_folder_path = os.path.join(
        "logs", "run_evaluation", run_id, "test", problem.instance_id, "test_output.txt"
    )
    if not os.path.exists(input_folder_path):
        test_results = run_tests(
            instance_ids=[problem.instance_id],
            model_patches=[patch if patch else NOOP_PATCH],
            max_workers=1,
            run_id=run_id,
            regression_test_file=regression_test_file,
        instances_to_run=[problem.instance_id],
        timeout=100.0,
            dataset_name="princeton-nlp/SWE-bench_Verified",
        )
    eval_sm, _ = get_logs_eval(input_folder_path)
    passing_tests = []
    for test_name, status in eval_sm.items():
        if status == "PASSED":
            passing_tests.append(test_name)

    # Clean up the temporary file if it was created
    if chosen_tests is not None and os.path.exists(regression_test_file):
        os.unlink(regression_test_file)

    return passing_tests



def run_regression_test_execution_for_patches(
    patches: list[PatchInfo], problem: SWEBenchProblem, config: RegressionTestConfig
) -> RegressionTestExecutionResults:
    chosen_tests = get_chosen_tests(problem)
    passing_tests_on_empty_diff = get_passing_tests(problem)
    with ThreadPoolExecutor(max_workers=config.num_threads_per_problem) as executor:
        # Use a list to store futures instead of a dictionary with unhashable keys
        futures = []
        for patch in patches:
            future = executor.submit(
                get_passing_tests,
                problem=problem,
                patch=patch.patch,
                chosen_tests=chosen_tests,
            )
            futures.append(future)
        
        passing_tests_per_edit = [future.result() for future in futures]
        return RegressionTestExecutionResults(
            passing_tests_on_empty_diff=passing_tests_on_empty_diff,
            passing_tests_per_edit=passing_tests_per_edit,
            patch_data=patches,
        )


def run_regression_test_execution(
    problem: SWEBenchProblem, config: RegressionTestConfig
):
    trajectory_store = config.get_trajectory_store(problem)
    if trajectory_store.regression_test_execution_exists():
        print(f"Regression test execution already exists for {problem.instance_id}")
        return

    patches = [
        patch
        for patch in trajectory_store.load_generated_test_execution().patch_data
    ]
    results = run_regression_test_execution_for_patches(
        patches,
        problem,
        config,
    )
    print("Storing regression test execution results")
    trajectory_store.save_regression_test_execution(results)


@pydra.main(RegressionTestConfig)
def main(config: RegressionTestConfig):
    parallelize(
        partial(run_regression_test_execution, config=config),
        config.get_problems(),
        config.num_problems_in_parallel,
        desc="Running test execution",
    )




if __name__ == "__main__":
    main()
