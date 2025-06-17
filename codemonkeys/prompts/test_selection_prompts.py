from codemonkeys.swe_bench.swe_bench_verified import CodebaseFile, SWEBenchProblem
from codemonkeys.trajectory_data.structs import PatchInfo
from codemonkeys.prompts.shared_state_machine_prompts import format_files
from codemonkeys.prompts.selection_state_machine_prompts import format_candidate_fixes


def test_selection_prompt(
    problem: SWEBenchProblem,
    passing_tests: list[str],
):

    return f"""We are currently solving the following issue within our repository. Here is the issue text:
--- BEGIN ISSUE ---
{problem.problem_statement}
--- END ISSUE ---

Below are a list of existing tests in the repository.
```
{passing_tests}
```

Please identify the tests that should not be run after applying the patch to fix the issue.
These tests should be excluded as the original functionality may change due to the patch.

### Example
```
test1
test2
test5
```
Return only the selected tests.
"""