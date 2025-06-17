import os
import re
import hashlib
from pathlib import Path
from dataclasses import dataclass
from functools import cache
import datasets
from codemonkeys.swe_bench.codebase_content import file_content_dataset



@dataclass
class CodebaseFile:
    path: str
    content: str


class SWEBenchProblem:
    def __init__(self, problem_statement_or_row, repo_location: str = None, instance_id: str | None = None):
        # Check if we're using the dataset row version or the explicit parameters version
        if repo_location is None and isinstance(problem_statement_or_row, dict):
            # Dataset row version
            self._row = problem_statement_or_row
        else:
            # Explicit parameters version
            self._problem_statement = problem_statement_or_row
            self._repo_location = repo_location
            self._instance_id = instance_id
            
            # Get the base commit from the repo location
            self._base_commit = self._get_base_commit()
            self._repo = None

    def is_dataset_row(self) -> bool:
        return hasattr(self, '_row')
    
    @property
    def instance_id(self) -> str:
        if hasattr(self, '_row'):
            return self._row["instance_id"]
        
        if self._instance_id is None:
            self._instance_id = hashlib.md5(self._problem_statement.encode()).hexdigest()
        return self._instance_id

    @property
    def problem_statement(self) -> str:
        if hasattr(self, '_row'):
            return self._row["problem_statement"]
        return self._problem_statement

    @property
    def repo_location(self) -> str:
        if hasattr(self, '_row'):
            raise AttributeError("repo_location not available in dataset row version")
        return self._repo_location
    
    @property
    def base_commit(self) -> str:
        if hasattr(self, '_row'):
            return self._row["base_commit"]
        return self._base_commit
    
    @property
    def repo(self) -> str:
        if hasattr(self, '_row'):
            return self._row["repo"]
        
        if self._repo is None:
            self._repo = self.get_repo(self._repo_location)
        return self._repo
    
    @property
    def version(self):
        if hasattr(self, '_row'):
            return self._row["version"]
        return 0  # TODO: get version from repo
    
    @property
    def gold_patch(self) -> str:
        if hasattr(self, '_row'):
            return self._row["patch"]
        raise AttributeError("gold_patch not available in explicit parameters version")
    
    @property
    def gold_test_patch(self):
        if hasattr(self, '_row'):
            return self._row["test_patch"]
        raise AttributeError("gold_test_patch not available in explicit parameters version")
    
    def _get_base_commit(self) -> str:
        # Extract the base commit from the repo location
        # This assumes the repo location contains the base commit information
        # If not available, return an empty string or handle accordingly
        try:
            # Implementation depends on how the base commit is stored in the repo location
            # This is a placeholder implementation
            if os.path.exists(os.path.join(self._repo_location, ".git", "HEAD")):
                with open(os.path.join(self._repo_location, ".git", "HEAD"), "r") as f:
                    head_content = f.read().strip()
                    if head_content.startswith("ref: "):
                        ref_path = head_content[5:]
                        with open(os.path.join(self._repo_location, ".git", ref_path), "r") as ref_file:
                            return ref_file.read().strip()
                    return head_content
            return ""
        except Exception:
            return ""
    
    def get_repo(self, directory: str) -> str:
        git_config_path = os.path.join(directory, '.git', 'config')
        if not os.path.exists(git_config_path):
            return ""
        try:
            with open(git_config_path, 'r', encoding='latin-1') as f:
                config = f.read()
            match = re.search(r'\[remote "origin"\].*?url\s*=\s*(.+)', config, re.DOTALL)
            return match.group(1).strip() if match else ""
        except Exception as e:
            print(f"Error getting project: {e}")
            return ""
    
    def all_file_paths(self) -> list[str] | set[str]:
        if hasattr(self, '_row'):
            content_ds = file_content_dataset()
            return {file.path for file in content_ds.instance_id_to_files[self.instance_id]}
        
        # Get all files recursively, including those in subdirectories
        result = []
        for root, dirs, files in os.walk(self._repo_location):
            for file in files:
                # Get the relative path from the repo location
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, self._repo_location)
                result.append(rel_path)
        return result

    def get_file(self, path: str) -> CodebaseFile:
        if hasattr(self, '_row'):
            content_ds = file_content_dataset()
            for file in content_ds.instance_id_to_files[self.instance_id]:
                if file.path == path:
                    return CodebaseFile(
                        path=path, content=content_ds.hash_to_content[file.content_hash]
                    )
            raise ValueError(f"{path} not found in {self.instance_id}")
        
        with open(f"{self._repo_location}/{path}", "r") as f:
            return CodebaseFile(path=path, content=f.read())

    def file_exists(self, path: str) -> bool:
        if hasattr(self, '_row'):
            content_ds = file_content_dataset()
            return path in [
                file.path for file in content_ds.instance_id_to_files[self.instance_id]
            ]
        
        return os.path.exists(f"{self._repo_location}/{path}")
    
    def get_test_command(self) -> list[str]:
        return get_test_command(self)


def get_test_command(problem: SWEBenchProblem) -> str:
    if "django" in problem.repo:
        return "./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1"
    if "django-cms" in problem.repo:
        return "./tests/runtests.py --verbosity 2"
    if "seaborn" in problem.repo:
        return "pytest --no-header -rA"
    if "astropy" in problem.repo:
        return "pytest -rA -vv -o console_output_style=classic --tb=no"
    if "sphinx" in problem.repo:
        return "tox --current-env -epy39 -v --"
    if "sympy" in problem.repo:
        return "bin/test -C --verbose"
    # Default to pytest
    return "pytest -rA"

@cache
def _load_dataset():
    dataset = datasets.load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
    return dataset

def problems() -> list[SWEBenchProblem]:
    dataset = _load_dataset()

    assert isinstance(dataset, datasets.Dataset)
    return [SWEBenchProblem(row) for row in dataset]


def _problem_by_id() -> dict[str, SWEBenchProblem]:
    return {problem.instance_id: problem for problem in problems()}


def get_problem(instance_id: str):
    return _problem_by_id()[instance_id]
